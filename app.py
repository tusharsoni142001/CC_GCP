from flask import Flask, request, jsonify
import json
import os
from dotenv import load_dotenv
from github_analyzer import analyze_commit
from github_release_analyzer import generate_release_note
from CustomException import *
import asyncio
import os
import certifi

# Set certificate path from certifi
os.environ['SSL_CERT_FILE'] = certifi.where()
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()

load_dotenv()

app = Flask(__name__)


@app.route('/webhook', methods=['POST'])
def webhook():
    # Checking event type from the request headers
    event_type = request.headers.get('X-Github-Event')
    if event_type != 'push':
        return jsonify({'message': 'Event not supported'}), 400
    
    # Parsing the json from request
    payload = request.json

    try:
        # Extracting repository information
        repo_owner = payload['repository']['owner']['name']
        repo_name = payload['repository']['name']

        # Extracting branch name
        ref = payload.get('ref', '')
        branch_name = ref.replace('refs/heads/', '')
    except KeyError as e:
        return jsonify({
            'message': f'Missing required field in webhook payload',
            'error': f'Key {str(e)} not found in payload'
        }), 400
    except Exception as e:
        return jsonify({
            'message': 'Invalid webhook payload',
            'error': str(e)
        }), 400

    print(f"Received a push event for {repo_owner}/{repo_name}")

    # Process each commit in the payload
    commits = payload.get('commits', [])
    results = []
    errors = []  # Track errors without stopping processing

    for commit in commits:
        try:
            commit_sha = commit.get('id')
            commit_message = commit.get('message', '')

            # Skipping the merge commits
            if commit_message.startswith('Merge'):
                print(f"Skipping merge commit {commit_sha}")
                continue

            # Analyzing the commit using the analyze_commit function
            result = asyncio.run(analyze_commit(repo_owner, repo_name, commit_sha, branch_name))
            
            if result:
                results.append({
                    "commit": commit_sha,
                    "file_path": result
                })
        except CommitNotFoundError as e:
            errors.append({
                "commit": commit_sha,
                "error": str(e),
                "type": "not_found"
            })
        except GitHubAPIError as e:
            errors.append({
                "commit": commit_sha,
                "error": str(e),
                "type": "github_api"
            })
        except GoogleCloudStorageError as e:
            errors.append({
                "commit": commit_sha,
                "error": str(e),
                "type": "storage"
            })
        except AnalyzerError as e:
            errors.append({
                "commit": commit_sha,
                "error": str(e),
                "type": "analyzer"
            })
        except Exception as e:
            errors.append({
                "commit": commit_sha,
                "error": f"Unexpected error: {str(e)}",
                "type": "unknown"
            })
    
    # Determine response status based on results
    if not results and errors:
        # If all commits failed, return an error status
        # Pick status code based on the first error type for simplicity
        status_code = 500
        if any(e["type"] == "not_found" for e in errors):
            status_code = 404
        elif any(e["type"] == "analyzer" for e in errors):
            status_code = 400
            
        return jsonify({
            "message": "Webhook processing encountered errors",
            "errors": errors,
            "repo_owner": repo_owner,
            "repo_name": repo_name
        }), status_code
    
    # Return both successes and errors
    return jsonify({
        "message": "Webhook received and processed",
        "commits_analyzed": len(results),
        "results": results,
        "errors": errors if errors else None,
        "repo_owner": repo_owner,
        "repo_name": repo_name
    }), 200
    

@app.route('/release-webhook', methods=['POST'])
def release_webhook():
    event_type = request.headers.get('X-Github-Event')

    if event_type != 'release':
        return jsonify({'message': 'Event not supported'}), 400
        
    payload = request.json

    try:
        # Extract release information
        repo_owner = payload['repository']['owner']['login']
        repo_name = payload['repository']['name']
        release_tag = payload['release']['tag_name']
        release_name = payload['release']['name']
        release_body = payload['release']['body']
        created_at = payload['release']['created_at']
    except KeyError as e:
        return jsonify({
            'message': f'Missing required field in release webhook payload',
            'error': f'Key {str(e)} not found in payload'
        }), 400

    try:
        release_note_path = asyncio.run(generate_release_note(
            repo_owner, repo_name, release_tag, release_name, release_body, created_at
        ))

        return jsonify({
            'message': 'Release note generated successfully',
            'release': release_tag,
            'path': release_note_path
        }), 200
    except CommitNotFoundError as e:
        return jsonify({
            'message': str(e),
            'release': release_tag,
            'error_type': 'commit_not_found'
        }), 404
    except GitHubAPIError as e:
        return jsonify({
            'message': str(e),
            'release': release_tag,
            'error_type': 'github_api'
        }), 503  # Service Unavailable for API errors
    except GoogleCloudStorageError as e:
        return jsonify({
            'message': str(e),
            'release': release_tag,
            'error_type': 'storage'
        }), 500
    except AnalyzerError as e:
        return jsonify({
            'message': str(e),
            'release': release_tag,
            'error_type': 'analyzer'
        }), 400
    except Exception as e:
        return jsonify({
            'message': f'Unexpected error: {str(e)}',
            'release': release_tag,
            'error_type': 'unknown'
        }), 500
    

@app.route('/', methods=['GET'])
def home():
    """Simple endpoint to verify the server is running"""
    return "GitHub Code Documentation Webhook is running!"


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)