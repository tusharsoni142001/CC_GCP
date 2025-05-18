import json
import os
import requests
import datetime
from pathlib import Path
from dotenv import load_dotenv
from langchain.prompts import PromptTemplate
from langchain_groq import ChatGroq
# from langchain.chains import LLMChain
from google.cloud import storage
from CustomException import *
import certifi
from httpx import Client
from summarizer import summarize_with_llm_async





load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# OUTPUT_DIR = os.getenv("OUTPUT_DIR", "./output")
bucket_name = os.getenv("BUCKET_NAME")
key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")


#llm context awarness implementation

#Extract project readme files from repo to understand the project goal or purpose
async def get_repository_readme_async(GITHUB_OWNER, GITHUB_REPO):
    """Get the README content to understand project purpose"""
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}


    # Try common README filenames
    for filename in ["README.md", "README.txt", "README", "Readme.md"]:
            url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{filename}"
            response = requests.get(url, headers=headers)
            
            if response.status_code == 200:
                content = response.json().get("content", "")
                if content:
                    # GitHub returns content as base64 encoded
                    import base64
                    # print(f"Successfully retrieved redme file: {base64.b64decode(content).decode('utf-8')}")
                    return base64.b64decode(content).decode('utf-8'),"readme"
    
    return "No README found"

#Function to get changed files in current commit
def get_changed_files_from_commit(commit_data):
    """Extract list of files changed in this commit"""
    changed_files = []
    
    if "files" in commit_data:
        for file in commit_data["files"]:
            file_path = file.get("filename", "")
            if file_path:
                changed_files.append(file_path)
    
    return changed_files

# Function to get previous commits for a specific file
def get_previous_commits_for_file(repo_owner, repo_name, file_path, current_commit_sha):
    """Get the 2 most recent commits that modified a specific file before current commit"""
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    
    url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/commits?path={file_path}&per_page=5"
    
    try:
        response = requests.get(url, headers=headers)
        
        if response.status_code != 200:
            print(f"Error fetching commits for {file_path}: {response.status_code}")
            return []
            
        commits = response.json()
        previous_commits = []
        
        for commit in commits:
            # Skip the current commit
            if commit["sha"] == current_commit_sha:
                continue
                
            previous_commits.append(commit["sha"])
            
            # Only keep the 2 most recent previous commits
            if len(previous_commits) >= 2:
                break
                
        return previous_commits
    except requests.exceptions.RequestException as e:
        print(f"Error connecting to GitHub API: {str(e)}")
        return []
    
#Function to find commit documentation in GCS bucket
def find_commit_documentation_in_gcs(bucket_name, repo_name, commit_sha):
    """Find documentation for a specific commit in GCS bucket"""
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    
    # List all blobs in the bucket
    blobs = list(bucket.list_blobs())
    
    for blob in blobs:
        if commit_sha in blob.name and repo_name in blob.name:
            # Found the documentation
            with blob.open("r") as f:
                return f.read()
    
    return None


# Fetching previous commits for changed files and summarizing documentation
async def get_previous_documentation_for_files(repo_owner, repo_name, changed_files, current_commit_sha, bucket_name):
    """Get and summarize documentation from previous commits for changed files"""
    # Track file-to-commits mapping
    file_to_commits = {}
    
    # Step 1: First identify all relevant commits for each file
    for file_path in changed_files[:5]:  # Limit to 5 files
        previous_commits = get_previous_commits_for_file(repo_owner, repo_name, file_path, current_commit_sha)
        file_to_commits[file_path] = previous_commits[:2]  # Limit to 2 commits per file
    
    # Step 2: Get unique commits across all files
    all_unique_commits = set()
    for commits in file_to_commits.values():
        all_unique_commits.update(commits)
    
    # Step 3: Fetch documentation for unique commits only
    commit_to_docs = {}
    for commit_sha in all_unique_commits:
        doc = find_commit_documentation_in_gcs(bucket_name, repo_name, commit_sha)
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

#Format the summaries into a context string
def format_previous_documentation_context(previous_docs):
    """Format previous documentation summaries into a context string"""
    if not previous_docs:
        return "No previous documentation available for the changed files."
    
    context = "## Previous Documentation for Changed Files\n\n"
    
    for file_path, summary in previous_docs.items():
        context += f"### {file_path}\n\n"
        context += f"{summary}\n\n"
    
    return context

# Configure llm
def setup_llm():

    # Pass the preconfigured client to ChatGroq
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

Repository: {repo_name}
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
        input_variables=["repo_name", "commit_sha", "author", "message", "project_context", "previous_documentation", "diff"],
        template=prompt_text
    )

    return prompt | llm

    #get commit details using github api
def get_commit_details(GITHUB_OWNER,GITHUB_REPO, COMMIT_SHA):

    
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"  # Explicitly request v3 API
    }

    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/commits/{COMMIT_SHA}"
    
    try:
        response = requests.get(url,headers=headers)

        if response.status_code == 200:
            print(f"Successfully retrieved commit details for {COMMIT_SHA} in {GITHUB_REPO}.")
            return response.json()
        elif response.status_code == 404:
            raise CommitNotFoundError(f"Commit {COMMIT_SHA} not found in {GITHUB_REPO}.")
        else:
            raise GitHubAPIError (f"Error getting commit details: {response.status_code} - {response.text}")
    except requests.exceptions.RequestException as e:
        raise GitHubAPIError(f"Error connecting to GitHub API: {str(e)}")

    #get commit diff using github api
def get_commit_diff(GITHUB_OWNER,GITHUB_REPO, COMMIT_SHA):
    headers = {"Authorization": f"token {GITHUB_TOKEN}",
               "Accept": "application/vnd.github.v3.diff"}

    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/commits/{COMMIT_SHA}"    

    try:
        response = requests.get(url, headers=headers)

        if response.status_code == 200:
                # Make sure we're properly parsing the JSON response
                print(f"Successfully retrieved commit diff for {COMMIT_SHA} in {GITHUB_REPO}.")
                return response.text
        elif response.status_code == 404:
            raise CommitNotFoundError(f"Commit {COMMIT_SHA} not found in {GITHUB_REPO}.")
        else:
            raise GitHubAPIError (f"Error getting commit details: {response.status_code} - {response.text}")
    except requests.exceptions.RequestException as e:
        raise GitHubAPIError(f"Error connecting to GitHub API: {str(e)}")
    
    #save explanation to gcs bucket
def upload_to_gcs(GITHUB_OWNER,GITHUB_REPO, COMMIT_SHA, bucket_name, blob_name,author_name,author_email,commit_date,commit_message,explanation,branch_name):
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)

        blob.content_type = "text/plain"

        try:
            with blob.open("w") as f:
                f.write(f"Repository: {GITHUB_OWNER}/{GITHUB_REPO}\n")
                f.write(f"Commit: {COMMIT_SHA}\n")
                f.write(f"Branch: {branch_name}\n")
                f.write(f"Author: {author_name} <{author_email}>\n")
                f.write(f"Date: {commit_date}\n")
                f.write(f"Message: {commit_message}\n")
                f.write("\n\n")
                f.write("*"*80+"\n\n")
                f.write(explanation)
        except GoogleCloudStorageError as e:
            raise GoogleCloudStorageError(f"Error uploading to GCS: {e}")
        else:
            return f"gs://{bucket_name}/{blob_name}"

    #analyze github commit 
async def analyze_commit(GITHUB_OWNER,GITHUB_REPO, COMMIT_SHA,branch_name):

    try:
        commit_data= get_commit_details(GITHUB_OWNER, GITHUB_REPO, COMMIT_SHA)
    except CommitNotFoundError as e:
        raise

    if not commit_data:
        print(f"Could not analyze commit {COMMIT_SHA} in {GITHUB_REPO}.")
        raise AnalyzerError(f"Could not analyze commit {COMMIT_SHA} in {GITHUB_REPO}.")
    
    # Check if commit_data is a dictionary
    if not isinstance(commit_data, dict):
        print(f"Error: Expected dictionary but got {type(commit_data)}")
        return commit_data
    
    try:
        commit_diff = get_commit_diff(GITHUB_OWNER, GITHUB_REPO, COMMIT_SHA)
    except CommitNotFoundError as e:
        raise

    if not commit_diff:
        print(f"Could not analyze commit {COMMIT_SHA} in {GITHUB_REPO}.")
        raise AnalyzerError(f"Could not analyze commit {COMMIT_SHA} in {GITHUB_REPO}.")
    
    #commit data
    try:
        author_name = commit_data['commit']['author']['name']
        author_email = commit_data['commit']['author']['email']
        commit_date = commit_data['commit']['author']['date']
        commit_message = commit_data['commit']['message']
    except KeyError as e:
        print(f"Error extracting commit metadata: Missing key {e}")
        print(f"Available keys: {commit_data.keys() if isinstance(commit_data, dict) else 'Not a dictionary'}")
        return None

    #Gather project context
    readme_content = await get_repository_readme_async(GITHUB_OWNER, GITHUB_REPO)
    project_context = await summarize_with_llm_async(readme_content, "readme")
    print(f"Project context: {project_context}")
    # Get changed files and previous documentation
    changed_files = get_changed_files_from_commit(commit_data)
    print(f"Found {len(changed_files)} changed files in this commit")
    
    # Get and summarize previous documentation
    previous_docs = await get_previous_documentation_for_files(
        GITHUB_OWNER, GITHUB_REPO, changed_files, COMMIT_SHA, bucket_name
    )

    previous_docs_context = format_previous_documentation_context(previous_docs)
    print(f"Retrieved and summarized previous documentation")


    chain = setup_llm()

    try:
        response = chain.invoke({
            "repo_name": GITHUB_REPO,
            "commit_sha": COMMIT_SHA,
            "author": author_name,
            "message": commit_message,
            "project_context": project_context,
            "previous_documentation": previous_docs_context,
            "diff": commit_diff
        })

        if hasattr(response, "content"):
            explanation = response.content
        elif hasattr(response,"text:"):
            explanation = response.text
        else:
            explanation = str(response)
    
    except Exception as e:
        raise (f"Error generating explanation: {e}")

       
    
    #save explanation to file
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    
    if bucket_name:
        try:
            blob_name = f"{branch_name}/commits/{timestamp}_{GITHUB_REPO}_{COMMIT_SHA}.txt"
            gcs_path = upload_to_gcs(GITHUB_OWNER,GITHUB_REPO, COMMIT_SHA, bucket_name,blob_name,author_name,author_email,commit_date,commit_message,explanation,branch_name)
            return gcs_path
        except Exception as e:
            raise GoogleCloudStorageError(f"Error uploading to GCS: {e}")
    else:
        raise GoogleCloudStorageError("No GCS bucket found")
    

