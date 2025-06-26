# gitlab_analyzer.py
import json
import os
import requests
import datetime
from pathlib import Path
from dotenv import load_dotenv
from langchain.prompts import PromptTemplate
from langchain_groq import ChatGroq
from google.cloud import storage
from CustomException import *
import certifi
from httpx import Client
from utils import summarize_with_llm_async, get_repository_readme_gitlab

load_dotenv()

GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
bucket_name = os.getenv("GITLAB_COMMIT_BUCKET")
key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

def get_changed_files_from_gitlab_commit(project_id, commit_sha):
    """Extract list of files changed in this commit from GitLab API"""
    headers = {"Authorization": f"Bearer {GITLAB_TOKEN}"}
    url = f"https://gitlab.kazan.myworldline.com/api/v4/projects/{project_id}/repository/commits/{commit_sha}/diff"
    
    try:
        response = requests.get(url, headers=headers)
        
        if response.status_code != 200:
            raise GitLabAPIError(f"Error fetching commit diff: {response.status_code} - {response.text}")
            
        diff_data = response.json()
        changed_files = []
        
        for file_diff in diff_data:
            file_path = file_diff.get("new_path", "")
            if file_path:
                changed_files.append(file_path)
        
        return changed_files
    except requests.exceptions.RequestException as e:
        raise GitLabAPIError(f"Error connecting to GitLab API: {str(e)}")

def get_previous_commits_for_file_gitlab(project_id, file_path, current_commit_sha):
    """Get the 2 most recent commits that modified a specific file before current commit in GitLab"""
    headers = {"Authorization": f"Bearer {GITLAB_TOKEN}"}
    
    url = f"https://gitlab.kazan.myworldline.com/api/v4/projects/{project_id}/repository/commits?path={file_path}&per_page=5"
    
    try:
        response = requests.get(url, headers=headers)
        
        if response.status_code != 200:
            print(f"Error fetching commits for {file_path}: {response.status_code}")
            return []
            
        commits = response.json()
        previous_commits = []
        
        for commit in commits:
            # Skip the current commit
            if commit["id"] == current_commit_sha:
                continue
                
            previous_commits.append(commit["id"])
            
            # Only keep the 2 most recent previous commits
            if len(previous_commits) >= 2:
                break
                
        return previous_commits
    except requests.exceptions.RequestException as e:
        print(f"Error connecting to GitLab API: {str(e)}")
        return []

async def get_previous_documentation_for_files_gitlab(project_id, project_name, changed_files, current_commit_sha, bucket_name):
    """Get and summarize documentation from previous commits for changed files in GitLab"""
    # Track file-to-commits mapping
    file_to_commits = {}
    
    # Step 1: First identify all relevant commits for each file
    for file_path in changed_files[:5]:  # Limit to 5 files
        previous_commits = get_previous_commits_for_file_gitlab(project_id, file_path, current_commit_sha)
        file_to_commits[file_path] = previous_commits[:2]  # Limit to 2 commits per file
    
    # Step 2: Get unique commits across all files
    all_unique_commits = set()
    for commits in file_to_commits.values():
        all_unique_commits.update(commits)
    
    # Step 3: Fetch documentation for unique commits only
    commit_to_docs = {}
    for commit_sha in all_unique_commits:
        doc = find_commit_documentation_in_gcs(bucket_name, project_name, commit_sha)
        if doc:
            commit_to_docs[commit_sha] = doc
    
    # Step 4: Create summaries for each file based on its commits
    previous_docs = {}
    for file_path, commits in file_to_commits.items():
        file_docs = []
        for commit_sha in commits:
            if commit_sha in commit_to_docs:
                file_docs.append(f"Documentation for commit {commit_sha[:7]}:\n{commit_to_docs[commit_sha]}")
        
        if file_docs:
            combined_doc = "\n\n---\n\n".join(file_docs)
            summary = await summarize_with_llm_async(combined_doc, "documentation")
            previous_docs[file_path] = summary
    
    return previous_docs

def format_previous_documentation_context_gitlab(previous_docs):
    """Format previous documentation summaries into a context string for GitLab"""
    if not previous_docs:
        return "No previous documentation available for the changed files."
    
    context = "## Previous Documentation for Changed Files\n\n"
    
    for file_path, summary in previous_docs.items():
        context += f"### {file_path}\n\n"
        context += f"{summary}\n\n"
    
    return context

def find_commit_documentation_in_gcs(bucket_name, project_name, commit_sha):
    """Find documentation for a specific commit in GCS bucket"""
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    
    # List all blobs in the bucket
    blobs = list(bucket.list_blobs())
    
    for blob in blobs:
        if commit_sha in blob.name and project_name in blob.name:
            # Found the documentation
            with blob.open("r") as f:
                return f.read()
    
    return None

def setup_llm_gitlab():
    
    """Configure LLM for GitLab commit analysis"""

    llm = ChatGroq(
        groq_api_key=GROQ_API_KEY,
        model_name="llama-3.3-70b-versatile",
        temperature=0.2
    )

    prompt_text = """
                You are a Technical Documentation Specialist who creates concise, practical release documentation from code changes.

Analyze the following commit diff and generate documentation that:

1. STRUCTURE:
   - Use a clear main heading summarizing the change
   - Use appropriate subheadings for different components changed (API endpoints, functions, etc.)
   - Format code snippets, endpoints, and parameters consistently with proper code formatting

2. KEY CONTENT TO IDENTIFY AND DOCUMENT (when present):
   - New/Modified API Endpoints: Include the full path, method (GET/POST/etc.), and a 2-3 sentence description
   - New Functions/Methods: Include function name, purpose, and a brief usage example
   - Configuration Changes: Document new settings, default values, and effects
   - Database Changes: Note schema updates, migrations, or data structure changes
   - UI Components: Document new UI elements or significant visual changes

3. FOR EACH KEY ELEMENT, INCLUDE (in 3-4 concise sentences max):
   - What it does/purpose
   - Required parameters/payload (if applicable)
   - Basic usage example or pattern
   - Return values or response format (for APIs)

4. WRITING GUIDELINES:
   - Be extremely concise - aim for documentation that fits on one screen
   - Focus on practical details developers need to know
   - Skip minor changes that don't affect functionality
   - Use technical but clear language

Project: {project_name}
Commit: {commit_sha}
Author: {author}
Commit Message: {message}

## Project Context:
{project_context}

## Previous Documentation for Changed Files:
{previous_documentation}

Changes:
{diff}

Generate documentation that highlights practical, actionable information about the changes. Documentation should be brief but include specific details like endpoint paths, parameter names, and return values when present in the changes.

Use the project context and previous documentation to better understand the purpose of the files and how they've evolved. Reference previous changes when relevant to provide continuity in the documentation.
                 """

    prompt = PromptTemplate(
        input_variables=["project_name", "commit_sha", "author", "message", "project_context", "previous_documentation", "diff"],
        template=prompt_text
    )

    return prompt | llm

def get_commit_details_gitlab(project_id, commit_sha):
    """Get commit details using GitLab API"""
    headers = {"PRIVATE-TOKEN": f"{GITLAB_TOKEN}"}
    url = f"https://gitlab.kazan.myworldline.com/api/v4/projects/{project_id}/repository/commits/{commit_sha}"
    
    try:
        response = requests.get(url, headers=headers)

        if response.status_code == 200:
            print(f"Successfully retrieved commit details for {commit_sha} in project {project_id}.")
            return response.json()
        elif response.status_code == 404:
            raise CommitNotFoundError(f"Commit {commit_sha} not found in project {project_id}.")
        else:
            raise GitLabAPIError(f"Error getting commit details: {response.status_code} - {response.text}")
    except requests.exceptions.RequestException as e:
        raise GitLabAPIError(f"Error connecting to GitLab API: {str(e)}")

def get_commit_diff_gitlab(project_id, commit_sha):
    """Get commit diff using GitLab API"""
    headers = {"Authorization": f"Bearer {GITLAB_TOKEN}"}
    url = f"https://gitlab.kazan.myworldline.com/api/v4/projects/{project_id}/repository/commits/{commit_sha}/diff"
    
    try:
        response = requests.get(url, headers=headers)

        if response.status_code == 200:
            # GitLab returns diff as an array of file diffs
            diff_data = response.json()
            
            # Convert to text format similar to GitHub diff
            diff_text = ""
            for file_diff in diff_data:
                diff_text += f"diff --git a/{file_diff.get('old_path')} b/{file_diff.get('new_path')}\n"
                diff_text += f"--- a/{file_diff.get('old_path')}\n"
                diff_text += f"+++ b/{file_diff.get('new_path')}\n"
                diff_text += file_diff.get('diff', '') + "\n\n"
                
            print(f"Successfully retrieved commit diff for {commit_sha} in project {project_id}.")
            return diff_text
        elif response.status_code == 404:
            raise CommitNotFoundError(f"Commit {commit_sha} not found in project {project_id}.")
        else:
            raise GitLabAPIError(f"Error getting commit diff: {response.status_code} - {response.text}")
    except requests.exceptions.RequestException as e:
        raise GitLabAPIError(f"Error connecting to GitLab API: {str(e)}")

def upload_to_gcs_gitlab(project_id, project_name, commit_sha, bucket_name, blob_name, author_name, commit_date, commit_message, explanation, branch_name):
    """Save explanation to GCS bucket for GitLab commits"""
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    blob.content_type = "text/plain"

    try:
        with blob.open("w") as f:
            f.write(f"Project: {project_id}/{project_name}\n")
            f.write(f"Commit: {commit_sha}\n")
            f.write(f"Branch: {branch_name}\n")
            f.write(f"Author: {author_name}\n")
            f.write(f"Date: {commit_date}\n")
            f.write(f"Message: {commit_message}\n")
            f.write("\n\n")
            f.write("*"*80+"\n\n")
            f.write(explanation)
    except Exception as e:
        raise GoogleCloudStorageError(f"Error uploading to GCS: {e}")
    else:
        return f"gs://{bucket_name}/{blob_name}"

async def analyze_gitlab_commit(project_id, project_name, commit_sha, branch_name, author_name, commit_message, commit_timestamp):
    """Analyze GitLab commit and generate documentation"""
    try:
        # Get commit details - might need this for additional metadata
        commit_data = get_commit_details_gitlab(project_id, commit_sha)
        
        if not commit_data:
            print(f"Could not analyze commit {commit_sha} in project {project_id}.")
            raise AnalyzerError(f"Could not analyze commit {commit_sha} in project {project_id}.")
        
        # Get commit diff
        commit_diff = get_commit_diff_gitlab(project_id, commit_sha)
        
        if not commit_diff:
            print(f"Could not get diff for commit {commit_sha} in project {project_id}.")
            raise AnalyzerError(f"Could not get diff for commit {commit_sha} in project {project_id}.")
        
        # Gather project context
        readme_content = await get_repository_readme_gitlab(project_id)
        project_context = await summarize_with_llm_async(readme_content, "readme")
        print(f"Project context: {project_context}")
        
        # Get changed files and previous documentation
        changed_files = get_changed_files_from_gitlab_commit(project_id, commit_sha)
        print(f"Found {len(changed_files)} changed files in this commit")
        
        # Get and summarize previous documentation
        previous_docs = await get_previous_documentation_for_files_gitlab(
            project_id, project_name, changed_files, commit_sha, bucket_name
        )
        
        previous_docs_context = format_previous_documentation_context_gitlab(previous_docs)
        print(f"Retrieved and summarized previous documentation")
        
        # Setup LLM and generate documentation
        chain = setup_llm_gitlab()
        
        try:
            response = chain.invoke({
                "project_name": project_name,
                "commit_sha": commit_sha,
                "author": author_name,
                "message": commit_message,
                "project_context": project_context,
                "previous_documentation": previous_docs_context,
                "diff": commit_diff
            })
            
            if hasattr(response, "content"):
                explanation = response.content
            elif hasattr(response, "text"):
                explanation = response.text
            else:
                explanation = str(response)
        
        except Exception as e:
            raise AnalyzerError(f"Error generating explanation: {e}")
        
        # Save explanation to GCS
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        
        if bucket_name:
            try:
                blob_name = f"{branch_name}/commits/{timestamp}_{project_name}_{commit_sha}.txt"
                gcs_path = upload_to_gcs_gitlab(
                    project_id, 
                    project_name, 
                    commit_sha, 
                    bucket_name,
                    blob_name,
                    author_name,
                    commit_timestamp,
                    commit_message,
                    explanation,
                    branch_name
                )
                return gcs_path
            except Exception as e:
                raise GoogleCloudStorageError(f"Error uploading to GCS: {e}")
        else:
             raise GoogleCloudStorageError("No GCS bucket found")
        
    except CommitNotFoundError as e:
        raise
    except GitLabAPIError as e:
        raise
    except GoogleCloudStorageError as e:
        raise
    except AnalyzerError as e:
        raise
    except Exception as e:
        raise AnalyzerError(f"Unexpected error while analyzing GitLab commit: {str(e)}")