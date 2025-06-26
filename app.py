from flask import Flask, request, jsonify
import json
import os
from dotenv import load_dotenv
from github_analyzer import analyze_commit
from github_release_analyzer import generate_release_note
from gitlab_analyzer import analyze_gitlab_commit
from gitlab_release_analyzer import fetch_gitlab_release_data, generate_gitlab_release_note 
from CustomException import *
import asyncio
import os
import certifi
# Set certificate path from certifi
os.environ['SSL_CERT_FILE'] = certifi.where()
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()

load_dotenv()

app = Flask(__name__)

@app.route('/gitlab-commit', methods=['POST'])
def gitlab_commit():
    """Handle GitLab webhook data"""
    # Parsing the json from request
    payload = request.json

    try:
        # Extract GitLab specific information from payload
        project_id = payload.get('project_id')
        project_name = payload.get('project_name')
        commit_sha = payload.get('commit_sha')
        commit_message = payload.get('commit_message')
        commit_timestamp = payload.get('commit_timestamp')
        author = payload.get('author')
        branch_name = payload.get('branch')
        
        # For GitLab, we typically use project_id as the owner and project_name as the repo
        # This is different from GitHub's owner/repo format
        if not project_id or not project_name or not commit_sha:
            return jsonify({
                'message': 'Missing required fields in GitLab commit payload',
                'error': 'Required fields: project_id, project_name, commit_sha'
            }), 400
    except Exception as e:
        return jsonify({
            'message': 'Invalid GitLab payload',
            'error': str(e)
        }), 400

    print(f"Received a GitLab push event for {project_id}/{project_name}")
    
    try:
        # Analyze the commit using GitLab analyzer
        result = asyncio.run(analyze_gitlab_commit(
            project_id, 
            project_name, 
            commit_sha, 
            branch_name,
            author,
            commit_message,
            commit_timestamp
        ))
        
        if result:
            return jsonify({
                "message": "GitLab webhook received and processed",
                "commit": commit_sha,
                "file_path": result,
                "project_id": project_id,
                "project_name": project_name
            }), 200
        else:
            return jsonify({
                "message": "Failed to analyze GitLab commit",
                "commit": commit_sha,
                "project_id": project_id,
                "project_name": project_name
            }), 500
    except CommitNotFoundError as e:
        return jsonify({
            "message": str(e),
            "commit": commit_sha,
            "error_type": "not_found"
        }), 404
    except GitLabAPIError as e:
        return jsonify({
            "message": str(e),
            "commit": commit_sha,
            "error_type": "gitlab_api"
        }), 503
    except GoogleCloudStorageError as e:
        return jsonify({
            "message": str(e),
            "commit": commit_sha,
            "error_type": "storage"
        }), 500
    except AnalyzerError as e:
        return jsonify({
            "message": str(e),
            "commit": commit_sha,
            "error_type": "analyzer"
        }), 400
    except Exception as e:
        return jsonify({
            "message": f"Unexpected error: {str(e)}",
            "commit": commit_sha,
            "error_type": "unknown"
        }), 500

@app.route('/webhook', methods=['POST'])
def github_webhook():
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


@app.route('/gitlab-release', methods=['POST'])
def gitlab_release():
    """Handle GitLab release webhook data with enhanced pipeline variables"""
    payload = request.json

    try:
        # Extract required fields
        project_id = payload.get('project_id')
        tag_name = payload.get('tag_name')
        
        if not project_id or not tag_name:
            return jsonify({
                'message': 'Missing required fields in GitLab release payload',
                'error': 'Required fields: project_id, tag_name'
            }), 400
        
        # Extract optional fields provided by pipeline
        project_name = payload.get('project_name')
        project_path = payload.get('project_path')
        commit_sha = payload.get('commit_sha')
        commit_timestamp = payload.get('commit_timestamp')
        commit_title = payload.get('commit_title')
        pipeline_id = payload.get('pipeline_id')
        pipeline_url = payload.get('pipeline_url')
        default_branch = payload.get('default_branch')
            
        # Fetch additional release information from GitLab API
        try:
            gitlab_data = fetch_gitlab_release_data(
                project_id, 
                tag_name,
                project_name=project_name,
                project_path=project_path,
                commit_sha=commit_sha,
                commit_timestamp=commit_timestamp
            )
            
            # Use provided project_name if available, otherwise use from API
            project_name = project_name or gitlab_data.get('project_name')
            release_name = gitlab_data.get('release_name', tag_name)
            release_body = gitlab_data.get('description', commit_title or '')
            created_at = gitlab_data.get('created_at', commit_timestamp)
            
            # Add pipeline context to the gitlab data
            gitlab_data['pipeline_id'] = pipeline_id
            gitlab_data['pipeline_url'] = pipeline_url
            gitlab_data['default_branch'] = default_branch
            
        except GitLabAPIError as e:
            # Fall back to pipeline data if API fails
            if project_name:
                # We can continue with minimal data if project_name is provided
                release_name = tag_name
                release_body = commit_title or ''
                created_at = commit_timestamp
                
                print(f"Warning: Using pipeline data only due to API error: {str(e)}")
            else:
                return jsonify({
                    'message': f'Error fetching release data from GitLab API: {str(e)}',
                    'error_type': 'gitlab_api'
                }), 503
        
    except Exception as e:
        return jsonify({
            'message': 'Invalid GitLab release payload',
            'error': str(e)
        }), 400

    try:
        # Pass all context data to the release note generator
        release_note_path = asyncio.run(generate_gitlab_release_note(
            project_id=project_id,
            project_name=project_name,
            release_tag=tag_name,
            release_name=release_name,
            release_body=release_body,
            created_at=created_at,
            context_data={
                'project_path': project_path,
                'commit_sha': commit_sha,
                'commit_timestamp': commit_timestamp,
                'commit_title': commit_title,
                'pipeline_id': pipeline_id,
                'pipeline_url': pipeline_url,
                'default_branch': default_branch
            }
        ))

        return jsonify({
            'message': 'GitLab release note generated successfully',
            'release': tag_name,
            'path': release_note_path,
            'project_name': project_name
        }), 200
    except CommitNotFoundError as e:
        return jsonify({
            'message': str(e),
            'release': tag_name,
            'error_type': 'commit_not_found'
        }), 404
    except GitLabAPIError as e:
        return jsonify({
            'message': str(e),
            'release': tag_name,
            'error_type': 'gitlab_api'
        }), 503
    except GoogleCloudStorageError as e:
        return jsonify({
            'message': str(e),
            'release': tag_name,
            'error_type': 'storage'
        }), 500
    except AnalyzerError as e:
        return jsonify({
            'message': str(e),
            'release': tag_name,
            'error_type': 'analyzer'
        }), 400
    except Exception as e:
        return jsonify({
            'message': f'Unexpected error: {str(e)}',
            'release': tag_name,
            'error_type': 'unknown'
        }), 500


@app.route('/release-webhook', methods=['POST'])
def github_release_webhook():
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
    return "GitHub/GitLab Code Documentation Webhook is running!"


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)