from flask import Flask, request, jsonify
import json
import os
from dotenv import load_dotenv
from github_analyzer import analyze_commit
from CustomException import *
from github_release_analyzer import generate_release_note


#Importing the analyze_commit function from github_analyzer module
# from github_analyzer import analyze_commit

#loading the environment variables from .env file
load_dotenv()

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():

    # Checking event type from the request headers
    # If the event type is not 'push', return a 400 response
    event_type=request.headers.get('X-Github-Event')
    if event_type!='push':
        return jsonify({'message':'Event not supported'}), 400
    

    #parsing the json from request
    payload = request.json

    #Extracting repository information
    repo_owner = payload['repository']['owner']['name']
    repo_name = payload['repository']['name']

    #Extracting branch name
    ref = payload.get('ref','')
    branch_name = ref.replace('refs/heads/','')


    print(f"Received a push event for {repo_owner}/{repo_name}")

    #Process each commit in the payload
    commits = payload.get('commits',[])
    results = []

    for commit in commits :
        try:
            commit_sha = commit.get('id')
            commit_message = commit.get('message')

            print(f"Processing commit: {commit_sha[:7]} - {commit_message.splitlines()[0]}")

            #Skipping the merge commits
            if commit_message.startswith('Merege'):
                print("Skipping merge commit")
                continue

            #Analyzing the commit using the analyze_commit function from github_analyzer module
            result = analyze_commit(repo_owner, repo_name, commit_sha, branch_name)
            
            if result:
                results.append({
                    "commit": commit_sha,
                    "file_path": result
                })
        except CommitNotFoundError as e:
            return jsonify({
                "message": str(e),
                "commit": commit_sha,
            }), 404
        except AnalyzerError as e:
            return jsonify({
                "message": str(e),
                "commit": commit_sha,
            }), 400
    
    return jsonify({
        "message": "Webhook received and processed",
        "commits_analyzed": len(results),
        "results": results,
        "repo_owner" : payload['repository']['owner']['name'],
        "repo_name" : payload['repository']['name']
    }),200

@app.route('/release-webhook', methods=['POST'])
def release_webhook():
    event_type = request.headers.get('X-Github-Event')

    if(event_type != 'release'):
        return jsonify({'message': 'Event not supported'}), 400
        
    payload = request.json

    #Extract release release information
    repo_owner = payload['repository']['owner']['login']
    repo_name = payload['repository']['name']
    release_tag = payload['release']['tag_name']
    release_name = payload['release']['name']
    release_body = payload['release']['body']
    created_at = payload['release']['created_at']

    try:
        release_note_path = generate_release_note(
            repo_owner, repo_name, release_tag, release_name, release_body, created_at
        )

        return jsonify({
            'message':'Release note generated successfully',
            'release': release_tag,
            'path':release_note_path
        }), 200
    except Exception as e:
        return jsonify({
            'message': str(e),
            'release': release_tag
        }), 500
    

@app.route('/', methods=['GET'])
def home():
    """Simple endpoint to verify the server is running"""
    return "GitHub Code Documentation Webhook is running!"

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
    
