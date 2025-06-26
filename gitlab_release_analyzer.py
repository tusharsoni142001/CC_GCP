# gitlab_release_analyzer.py
import json
import os
import requests
import datetime
from pathlib import Path
from dotenv import load_dotenv
from langchain.prompts import PromptTemplate
from langchain_groq import ChatGroq
from langchain.chains import LLMChain
from google.cloud import storage
from CustomException import *
from httpx import Client
from utils import summarize_with_llm_async, get_repository_readme_gitlab

load_dotenv()

GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "./output")
bucket_name_release = os.getenv("GITLAB_RELEASE_BUCKET")
bucket_name_commit = os.getenv("GITLAB_COMMIT_BUCKET")
key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

async def generate_gitlab_release_note(project_id, project_name, release_tag, release_name, release_body, created_at, context_data=None):
    """Generate release notes with enhanced context data from pipeline"""
    context_data = context_data or {}
    
    try:
        previous_tag = get_previous_release_tag_gitlab(project_id, release_tag)

        commits = get_commits_between_tags_gitlab(project_id, previous_tag, release_tag)

        commit_docs = []
        for commit in commits:
            try:
                commit_sha = commit["id"]
                doc_path = find_commit_documentation(bucket_name_commit, project_name, commit_sha)
                if doc_path:
                    doc_content = await read_gcs_file(bucket_name_commit, doc_path)
                    commit_docs.append({
                        'documentation': doc_content,
                    })
            except GoogleCloudStorageError as e:
                # Log but continue processing other commits
                print(f"Warning: Could not read documentation for commit {commit_sha}: {str(e)}")
                continue

        # Gather project context
        readme_content = await get_repository_readme_gitlab(project_id)
        project_context = await summarize_with_llm_async(readme_content, "readme")
        print(f"Project context: {project_context}")
            
        # Include pipeline context in release notes generation
        pipeline_context = ""
        if context_data.get('pipeline_id') and context_data.get('pipeline_url'):
            pipeline_context = f"\nGenerated from pipeline #{context_data.get('pipeline_id')}: {context_data.get('pipeline_url')}"
        
        if context_data.get('default_branch'):
            pipeline_context += f"\nDefault branch: {context_data.get('default_branch')}"
            
        release_notes = generate_note_gitlab(
            project_name,
            release_tag,
            release_name,
            previous_tag,
            release_body,
            commit_docs,
            project_context,
            pipeline_context
        )

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        blob_name = f"{project_name}/releases/{release_tag}/{timestamp}_release_note.md"

        # Include additional context in metadata
        metadata = {
            "project_id": project_id,
            "project_name": project_name,
            "project_path": context_data.get('project_path', ''),
            "release_tag": release_tag,
            "release_name": release_name,
            "created_at": created_at,
            "pipeline_id": context_data.get('pipeline_id', ''),
            "pipeline_url": context_data.get('pipeline_url', ''),
            "commit_sha": context_data.get('commit_sha', '')
        }

        return upload_to_gcs_release(bucket_name_release, blob_name, metadata, release_notes)

    except GitLabAPIError as e:
        raise
    except GoogleCloudStorageError as e:
        raise
    except AnalyzerError as e:
        raise
    except Exception as e:
        # Catch any unexpected exceptions and wrap them
        raise AnalyzerError(f"Unexpected error while generating GitLab release note: {str(e)}")

def get_previous_release_tag_gitlab(project_id, release_tag):
    url = f"https://gitlab.kazan.myworldline.com/api/v4/projects/{project_id}/releases"
    headers = {"Authorization": f"Bearer {GITLAB_TOKEN}"}

    try:
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            releases = response.json()
            release_sorted = sorted(releases, key=lambda x: x['released_at'])
            previous_tag = None

            for release in release_sorted:
                if release['tag_name'] == release_tag:
                    return previous_tag
                previous_tag = release['tag_name']

            return None
        elif response.status_code == 404:
            raise GitLabAPIError(f"Project {project_id} not found")
        else:
            raise GitLabAPIError(f"Failed to fetch releases: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        raise GitLabAPIError(f"Error connecting to GitLab API: {str(e)}")

def get_commits_between_tags_gitlab(project_id, previous_tag, release_tag):
    headers = {"Authorization": f"Bearer {GITLAB_TOKEN}"}

    try:
        if previous_tag is None:
            # If no previous tag, get the most recent commits
            url = f"https://gitlab.kazan.myworldline.com/api/v4/projects/{project_id}/repository/commits?ref_name={release_tag}&per_page=50"
        else:
            # Get commits between tags
            url = f"https://gitlab.kazan.myworldline.com/api/v4/projects/{project_id}/repository/compare?from={previous_tag}&to={release_tag}"

        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            if previous_tag is None:
                return data
            else:
                return data.get("commits", [])
        elif response.status_code == 404:
            if previous_tag is None:
                raise GitLabAPIError(f"Project {project_id} not found")
            else:
                raise GitLabAPIError(f"Cannot compare tags: {previous_tag} or {release_tag} not found")
        else:
            raise GitLabAPIError(f"Failed to fetch commits: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        raise GitLabAPIError(f"Error connecting to GitLab API: {str(e)}")

def find_commit_documentation(bucket_name, project_name, commit_sha):
    if not bucket_name:
        raise GoogleCloudStorageError("No commit documentation bucket specified")
        
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)

        blobs = bucket.list_blobs(prefix=f"")

        for blob in blobs:
            if commit_sha in blob.name and project_name in blob.name:
                return blob.name
        
        return None
    except Exception as e:
        raise GoogleCloudStorageError(f"Error finding commit documentation: {str(e)}")

async def read_gcs_file(bucket_name, blob_name):
    if not bucket_name or not blob_name:
        raise GoogleCloudStorageError("Missing bucket name or blob name")
        
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)

        if not blob.exists():
            return None
        
        with blob.open("r") as f:
            return await summarize_with_llm_async(f.read(), "documentation")
    except Exception as e:
        raise GoogleCloudStorageError(f"Error reading file from GCS: {str(e)}")

def generate_note_gitlab(project_name, release_tag, release_name, previous_tag, release_body, commit_docs, project_context, pipeline_context=""):
    if not GROQ_API_KEY:
        raise AnalyzerError("GROQ API key not configured")
        
    try:
        
        llm = ChatGroq(
            groq_api_key=GROQ_API_KEY,
            model_name="llama-3.3-70b-versatile",
            temperature=0.2
        )

        prompt_text = ''' You are a Technical Documentation Specialist tasked with creating comprehensive release notes.

        Generate detailed release notes for version {release_tag} of {project_name}. This release moves from previous version {previous_tag} to {release_tag}.
        
        The release is titled: {release_name}
        
        Original release description:
        {release_body}
        
        Based on the commits included in this release, create structured release notes with the following sections:
        1. Overview - Brief summary of the most important changes
        2. New Features - Detailed description of new functionality
        3. Improvements - Enhancements to existing features
        4. Bug Fixes - Issues that were resolved
        5. Breaking Changes (if any) - Changes requiring user action
        6. Technical Notes - Implementation details important for developers
        
        Here are the commits included in this release:
        {commit_docs}

        ## Project Context:
        {project_context}
        
        ## Pipeline Context:
        {pipeline_context}
        
        Format the release notes with clear Markdown headings, bullet points for individual changes, and code blocks where appropriate.
        Be specific about changes but maintain a professional tone suitable for both technical and non-technical readers.
        
        Ensure the release notes are easy to read and understand, with a focus on clarity and conciseness. And make sure to include the date of the release.
        Even if the commits don't have new features or improvements, then generate changes based on the commit messages.
        If there are no changes, then generate a message indicating that there are no changes in this release.

        Use the project context to better understand the purpose of the commits and how they've evolved. Reference previous changes when relevant to provide continuity in the release note.
        '''

        prompt = PromptTemplate(
            input_variables=["project_name", "release_tag", "release_name", "previous_tag", "release_body", "commit_docs", "project_context", "pipeline_context"],
            template=prompt_text
        )
        
        chain = prompt | llm
        
        response = chain.invoke({
            "project_name": project_name,
            "project_name": project_name,
            "release_tag": release_tag,
            "release_name": release_name,
            "previous_tag": previous_tag or "initial release",
            "release_body": release_body,
            "commit_docs": commit_docs,
            "project_context": project_context,
            "pipeline_context": pipeline_context
        })
        
        if hasattr(response, "content"):
            return response.content
        return str(response)
    except Exception as e:
        raise AnalyzerError(f"Error generating release notes: {str(e)}")

def upload_to_gcs_release(bucket_name, blob_name, metadata, release_notes):
    """
    Upload release notes to GCS with enhanced metadata from pipeline
    
    Args:
        bucket_name: The GCS bucket name
        blob_name: The path/name for the blob in the bucket
        metadata: Dictionary containing all metadata to attach to the file
        release_notes: The content of the release notes
    """
    if not bucket_name:
        raise GoogleCloudStorageError("No release notes bucket specified")
        
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.content_type = "text/markdown"

        # Clean metadata to ensure all values are strings
        clean_metadata = {}
        for key, value in metadata.items():
            if value is not None:
                clean_metadata[key] = str(value)

        blob.metadata = clean_metadata
        blob.upload_from_string(release_notes, content_type="text/markdown")

        return f"gs://{bucket_name}/{blob.name}"
    except Exception as e:
        raise GoogleCloudStorageError(f"Error uploading release notes to GCS: {str(e)}")
    

def fetch_gitlab_release_data(project_id, tag_name, project_name=None, project_path=None, commit_sha=None, commit_timestamp=None):
    """Fetch complete release data from GitLab API with pipeline context"""
    if not GITLAB_TOKEN:
        raise GitLabAPIError("GitLab API token not configured")
        
    headers = {"Authorization": f"Bearer {GITLAB_TOKEN}"}
    
    # Step 1: Get project details if not provided
    if not project_name or not project_path:
        project_url = f"https://gitlab.kazan.myworldline.com/api/v4/projects/{project_id}"
        try:
            project_response = requests.get(project_url, headers=headers)
            if project_response.status_code != 200:
                raise GitLabAPIError(f"Failed to fetch project details: {project_response.status_code}")
            
            project_data = project_response.json()
            project_name = project_name or project_data.get('name')
            project_path = project_path or project_data.get('path_with_namespace')
        except requests.exceptions.RequestException as e:
            raise GitLabAPIError(f"Error connecting to GitLab API for project details: {str(e)}")
    
    # Step 2: Get release details
    release_url = f"https://gitlab.kazan.myworldline.com/api/v4/projects/{project_id}/releases/{tag_name}"
    try:
        release_response = requests.get(release_url, headers=headers)
        if release_response.status_code == 200:
            release_data = release_response.json()
            return {
                'project_id': project_id,
                'project_name': project_name,
                'project_path': project_path,
                'tag_name': tag_name,
                'release_name': release_data.get('name', tag_name),
                'description': release_data.get('description', ''),
                'created_at': release_data.get('created_at'),
                'released_at': release_data.get('released_at'),
                'commit_sha': commit_sha or release_data.get('commit', {}).get('id')
            }
        elif release_response.status_code == 404:
            # Release might not exist yet, just get tag info
            tag_url = f"https://gitlab.kazan.myworldline.com/api/v4/projects/{project_id}/repository/tags/{tag_name}"
            tag_response = requests.get(tag_url, headers=headers)
            
            if tag_response.status_code != 200:
                # Just return basic info if we can't get tag details
                return {
                    'project_id': project_id,
                    'project_name': project_name,
                    'project_path': project_path,
                    'tag_name': tag_name,
                    'release_name': tag_name,
                    'description': '',
                    'created_at': commit_timestamp or datetime.datetime.now().isoformat(),
                    'commit_sha': commit_sha
                }
                
            tag_data = tag_response.json()
            return {
                'project_id': project_id,
                'project_name': project_name,
                'project_path': project_path,
                'tag_name': tag_name,
                'release_name': tag_name,
                'description': tag_data.get('message', ''),
                'created_at': tag_data.get('commit', {}).get('created_at') or commit_timestamp,
                'commit_sha': commit_sha or tag_data.get('commit', {}).get('id')
            }
        else:
            raise GitLabAPIError(f"Failed to fetch release details: {release_response.status_code}")
            
    except requests.exceptions.RequestException as e:
        raise GitLabAPIError(f"Error connecting to GitLab API for release details: {str(e)}")