from langchain_groq import ChatGroq
from langchain.prompts import PromptTemplate
import os
from dotenv import load_dotenv
from httpx import Client
import requests
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

async def summarize_with_llm_async(text, content_type, max_length=300):
    """Use LLM to intelligently summarize any text"""
    if not text or len(text) < max_length:
        return text
    
    # Use a faster/smaller model for summarization if available
    summarizer_llm = ChatGroq(
        groq_api_key=GROQ_API_KEY,
        model_name="llama-3.1-8b-instant",  # Smaller model for summarization
        temperature=0.1
    )
    
    # Different prompts for different content types
    prompts = {
        "readme": """Summarize this project README to capture the essential purpose, 
                    features and structure of the project in under 500 words:
                    
                    {text}
                    
                    SUMMARY:""",
                    
        "file_history": """Summarize the history of file changes below to highlight patterns 
                          and significant modifications in under 500 words:
                          
                          {text}
                          
                          SUMMARY:""",
                          
        "documentation": """Summarize this previous documentation to highlight the most relevant 
                           information for understanding code changes in under 500 words:
                           
                           {text}
                           
                           Keep Repository, Commit, Branch, Author, Date, Message heading as it is.
                           SUMMARY:"""
    }
    
    prompt_template = PromptTemplate(
        input_variables=["text"],
        template=prompts.get(content_type, prompts["documentation"])
    )
    
    chain = prompt_template | summarizer_llm
    
    response = chain.invoke({"text": text})
    
    if hasattr(response, "content"):
        return response.content
    return str(response)


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