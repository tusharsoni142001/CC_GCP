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

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "./output")
bucket_name_release = os.getenv("PROJECT_NAME")
bucket_name_commit = os.getenv("BUCKET_NAME")
key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

def generate_release_note(repo_owner, repo_name, release_tag, release_name, release_body, created_at):
    try:
        previous_tag = get_previous_release_tag(repo_owner, repo_name, release_tag)

        commits = get_commits_between_tags(repo_owner, repo_name, previous_tag, release_tag)

        commit_docs = []
        for commit in commits:
            try:
                commit_sha = commit["sha"]
                doc_path = find_commit_documentation(bucket_name_commit, repo_owner, repo_name, commit_sha)
                if doc_path:
                    doc_content = read_gcs_file(bucket_name_commit, doc_path)
                    commit_docs.append({
                        'documentation': doc_content,
                    })
            except GoogleCloudStorageError as e:
                # Log but continue processing other commits
                print(f"Warning: Could not read documentation for commit {commit_sha}: {str(e)}")
                continue
        
        release_notes = generate_note(
            repo_name,
            release_tag,
            release_name,
            previous_tag,
            release_body,
            commit_docs
        )

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        blob_name = f"{repo_name}/releases/{release_tag}/{timestamp}_release_note.md"

        return upload_to_gcs(bucket_name_release, blob_name, repo_owner, repo_name, release_tag, release_name, created_at, release_notes)

    except GitHubAPIError as e:
        raise
    except GoogleCloudStorageError as e:
        raise
    except AnalyzerError as e:
        raise
    except Exception as e:
        # Catch any unexpected exceptions and wrap them
        raise AnalyzerError(f"Unexpected error while generating release note: {str(e)}")

def get_previous_release_tag(repo_owner, repo_name, release_tag):
    url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/releases"
    headers = {"Authorization":f"token {GITHUB_TOKEN}"}

    try:
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            releases = response.json()
            release_sorted = sorted(releases, key=lambda x: x['created_at'])
            previous_tag = None

            for release in release_sorted:
                if release['tag_name'] == release_tag:
                    return previous_tag
                previous_tag = release['tag_name']

            return None
        elif response.status_code == 404:
            raise GitHubAPIError(f"Repository {repo_owner}/{repo_name} not found")
        else:
            raise GitHubAPIError(f"Failed to fetch releases: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        raise GitHubAPIError(f"Error connecting to GitHub API: {str(e)}")

def get_commits_between_tags(repo_owner, repo_name, previous_tag, release_tag):
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    try:
        if previous_tag is None:
            url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/commits?per_page=50"
        else:
            url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/compare/{previous_tag}...{release_tag}"

        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            if previous_tag is None:
                return data
            else:
                return data.get("commits", [])
        elif response.status_code == 404:
            if previous_tag is None:
                raise GitHubAPIError(f"Repository {repo_owner}/{repo_name} not found")
            else:
                raise GitHubAPIError(f"Cannot compare tags: {previous_tag} or {release_tag} not found")
        else:
            raise GitHubAPIError(f"Failed to fetch commits: {response.status_code} - {response.text}")
            
    except requests.exceptions.RequestException as e:
        raise GitHubAPIError(f"Error connecting to GitHub API: {str(e)}")
    
def find_commit_documentation(bucket_name, repo_owner, repo_name, commit_sha):
    if not bucket_name:
        raise GoogleCloudStorageError("No commit documentation bucket specified")
        
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)

        blobs = bucket.list_blobs(prefix=f"")

        for blob in blobs:
            if commit_sha in blob.name and repo_name in blob.name:
                return blob.name
        
        return None
    except Exception as e:
        raise GoogleCloudStorageError(f"Error finding commit documentation: {str(e)}")

def read_gcs_file(bucket_name, blob_name):
    if not bucket_name or not blob_name:
        raise GoogleCloudStorageError("Missing bucket name or blob name")
        
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)

        if not blob.exists():
            return None
        
        with blob.open("r") as f:
            return f.read()
    except Exception as e:
        raise GoogleCloudStorageError(f"Error reading file from GCS: {str(e)}")
    
def generate_note(repo_name, release_tag, release_name, previous_tag, release_body, commit_docs):
    if not GROQ_API_KEY:
        raise AnalyzerError("GROQ API key not configured")
        
    try:
        llm = ChatGroq(
            groq_api_key=GROQ_API_KEY,
            model_name="llama-3.3-70b-versatile",
            temperature=0.2
        )

        prompt_text = ''' You are a Technical Documentation Specialist tasked with creating comprehensive release notes.

        Generate detailed release notes for version {release_tag} of {repo_name}. This release moves from previous version {previous_tag} to {release_tag}.
        
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
        
        Format the release notes with clear Markdown headings, bullet points for individual changes, and code blocks where appropriate.
        Be specific about changes but maintain a professional tone suitable for both technical and non-technical readers.
        
        Ensure the release notes are easy to read and understand, with a focus on clarity and conciseness. And make sure to include the date of the release.
        Even if the commits don't have new features or improvements , then generate changes based on the commit messages.
        If there are no changes, then generate a message indicating that there are no changes in this release.
        '''

        prompt = PromptTemplate(
            input_variables=["repo_name", "release_tag", "release_name", "previous_tag", "release_body", "commit_docs"],
            template=prompt_text
        )
        
        chain = prompt | llm
        
        response = chain.invoke({
            "repo_name": repo_name,
            "release_tag": release_tag,
            "release_name": release_name,
            "previous_tag": previous_tag or "initial release",
            "release_body": release_body,
            "commit_docs": commit_docs
        })
        
        if hasattr(response, "content"):
            return response.content
        return str(response)
    except Exception as e:
        raise AnalyzerError(f"Error generating release notes: {str(e)}")

def upload_to_gcs(bucket_name, blob_name, repo_owner, repo_name, release_tag, release_name, created_at, release_notes):
    if not bucket_name:
        raise GoogleCloudStorageError("No release notes bucket specified")
        
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.content_type = "text/markdown"

        metadata = {
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "release_tag": release_tag,
            "release_name": release_name,
            "created_at": created_at,
        }

        blob.metadata = metadata
        blob.upload_from_string(release_notes, content_type="text/markdown")

        return f"gs://{bucket_name}/{blob.name}"
    except Exception as e:
        raise GoogleCloudStorageError(f"Error uploading release notes to GCS: {str(e)}")