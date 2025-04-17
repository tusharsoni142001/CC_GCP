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
bucket_name = os.getenv("BUCKET_NAME")
key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

#Ensure output directory exists
Path(OUTPUT_DIR).mkdir(exist_ok=True)

# Configure llm
def setup_llm():
    llm = ChatGroq(
        groq_api_key=GROQ_API_KEY,
        model_name="llama-3.3-70b-versatile",
        temperature=0.2
    )

    prompt_text = """
                You are a Technical Documentation Specialist responsible for creating professional, well-structured release documentation
                 from code changes.

Analyze the following commit diff and generate comprehensive documentation that:

1. STRUCTURE AND FORMAT:
   - Use a clear, descriptive main heading that summarizes the change
   - Organize content with proper hierarchical headings (H2, H3, etc.)
   - Include a brief overview/summary at the beginning
   - Separate distinct features or changes into their own sections
   - Use bullet points for lists of features, requirements, or options
   - Format code references, parameters, and technical terms consistently

2. CONTENT SECTIONS (include all relevant sections):
   - Overview: Brief summary of the changes
   - Purpose: Why these changes were made and what problems they solve
   - New Features/Changes: Detailed explanation of new functionality
   - Requirements: Any prerequisites or dependencies
   - Usage: How to use the changed/new functionality
   - Technical Notes: Important implementation details developers should know
   - Limitations or Known Issues: Any constraints or pending concerns
   - Related Changes: Connections to other parts of the system

3. WRITING STYLE:
   - Be concise but comprehensive
   - Use clear, direct language accessible to the intended audience
   - Explain technical concepts without unnecessary jargon

Repository: {repo_name}
Commit: {commit_sha}
Author: {author}
Commit Message: {message}

Changes:
{diff}

Generate professional documentation that follows a logical structure with proper headings and formatting. 
The documentation should be comprehensive enough to be immediately useful to both technical and non-technical stakeholders. 
Keep documentation length appropriately based on the significance of the changes.
                """

    prompt = PromptTemplate(
        input_variables=["repo_name", "commit_sha", "author", "message", "diff"],
        template=prompt_text
    )

    return prompt | llm

    #get commit details using github api
def get_commit_details(GITHUB_OWNER,GITHUB_REPO, COMMIT_SHA):
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"  # Explicitly request v3 API
    }

    # Print detailed debugging info
    # print(f"Making GitHub API request for commit details:")
    # print(f"  Repository: {GITHUB_OWNER}/{GITHUB_REPO}")
    # print(f"  Commit SHA: {COMMIT_SHA}")
    # print(f"  Token permissions: {'***' + GITHUB_TOKEN[-4:] if GITHUB_TOKEN else 'None'}")

    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/commits/{COMMIT_SHA}"

    response = requests.get(url,headers=headers)

    # print(response.content)

    if response.status_code == 200:
        print(f"Successfully retrieved commit details for {COMMIT_SHA} in {GITHUB_REPO}.")
        return response.json()
    elif response.status_code == 404:
        raise CommitNotFoundError(f"Commit {COMMIT_SHA} not found in {GITHUB_REPO}.")
    else:
        raise Exception (f"Error getting commit details: {response.status_code} - {response.text}")
    

    #get commit diff using github api
def get_commit_diff(GITHUB_OWNER,GITHUB_REPO, COMMIT_SHA):
    headers = {"Authorization": f"token {GITHUB_TOKEN}",
               "Accept": "application/vnd.github.v3.diff"}

    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/commits/{COMMIT_SHA}"    
    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        try:
            # Make sure we're properly parsing the JSON response
            print(f"Successfully retrieved commit diff for {COMMIT_SHA} in {GITHUB_REPO}.")
            return response.text
        except Exception as e:
            print(f"Error retrieving commit diff: {e}")
            return None
    else:
        return f"Error getting commit diff: {response.status_code} - {response.text}"
    
    #save explanation to gcs bucket
def upload_to_gcs(GITHUB_OWNER,GITHUB_REPO, COMMIT_SHA, bucket_name, blob_name,author_name,author_email,commit_date,commit_message,explanation,branch_name):
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)

        blob.content_type = "text/plain"

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
        
        return f"gs://{bucket_name}/{blob_name}"

    #analyze github commit 
def analyze_commit(GITHUB_OWNER,GITHUB_REPO, COMMIT_SHA,branch_name):

    try:
        commit_data= get_commit_details(GITHUB_OWNER, GITHUB_REPO, COMMIT_SHA)
    except CommitNotFoundError as e:
        raise

    if not commit_data:
        print(f"Could not analyze commit {COMMIT_SHA} in {GITHUB_REPO}.")
        return None
    
    # Debug - Print what we received
    print(f"Commit data type: {type(commit_data)}")
    
    # Check if commit_data is a dictionary
    if not isinstance(commit_data, dict):
        print(f"Error: Expected dictionary but got {type(commit_data)}")
        return commit_data
    
    commit_diff = get_commit_diff(GITHUB_OWNER, GITHUB_REPO, COMMIT_SHA)

    if not commit_diff:
        print(f"Could not analyze commit {COMMIT_SHA} in {GITHUB_REPO}.")
        return None
    
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

    chain = setup_llm()

    try:
        response = chain.invoke({
            "repo_name" : GITHUB_REPO,
            "commit_sha" : COMMIT_SHA,
            "author" : author_name,
            "message" : commit_message,
            "diff" : commit_diff
        })

        if hasattr(response, "content"):
            explanation = response.content
        elif hasattr(response,"text:"):
            explanation = response.text
        else:
            explanation = str(response)
    
    except Exception as e:
        print(f"Error generating explanation: {e}")
        return None
    
    #save explanation to file
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{GITHUB_REPO}_{COMMIT_SHA}.txt"
    # filepath = os.path.join(OUTPUT_DIR, filename)


    # with open(filepath, 'w', encoding='utf-8') as f:
    #     f.write(f"Repository: {GITHUB_OWNER}/{GITHUB_REPO}\n")
    #     f.write(f"Commit: {COMMIT_SHA}\n")
    #     f.write(f"Author: {author_name} <{author_email}>\n")
    #     f.write(f"Date: {commit_date}\n")
    #     f.write(f"Message: {commit_message}\n")
    #     f.write("\n\n")
    #     f.write("*"*80+"\n\n")
    #     f.write(explanation)
    
    #upload to gcp bucket
    
    if bucket_name:
        try:
            blob_name = f"{branch_name}/commits/{timestamp}_{GITHUB_REPO}_{COMMIT_SHA}.txt"
            gcs_path = upload_to_gcs(GITHUB_OWNER,GITHUB_REPO, COMMIT_SHA, bucket_name,blob_name,author_name,author_email,commit_date,commit_message,explanation,branch_name)
            return gcs_path
        except Exception as e:
            raise AnalyzerError(f"Error uploading to GCS: {e}")
    else:
        raise AnalyzerError("No GCS bucket found")
    

