from mcp.server.fastmcp import FastMCP
from tavily import TavilyClient
import os
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# Initialize MCP
mcp = FastMCP("web-search")

# Initialize Tavily
tavily_key = os.getenv("TAVILY_API_KEY")
tavily = TavilyClient(api_key=tavily_key) if tavily_key else None

@mcp.tool()
async def search_web(query: str, num_results: int = 5):
    """
    Search the web using Tavily (Optimized for AI Agents).
    Returns relevant snippets and content automatically.
    """
    if not tavily:
        return "Error: TAVILY_API_KEY not found in environment."

    try:
        print(f"Searching Tavily for: {query}")
        
        # 'search_depth="advanced"' does a deeper search and extraction
        response = tavily.search(
            query=query, 
            search_depth="advanced", 
            max_results=num_results,
            include_answer=True # Asks Tavily to generate a short answer
        )
        
        output = f"Search Query: {query}\n"
        
        # 1. Direct Answer (if available)
        if response.get('answer'):
            output += f"--- DIRECT ANSWER ---\n{response['answer']}\n\n"
            
        # 2. Search Results
        output += "--- SOURCES ---\n"
        for i, res in enumerate(response.get('results', [])):
            title = res.get('title', 'No Title')
            url = res.get('url', 'No URL')
            content = res.get('content', '')[:1000] # Truncate individual results
            
            output += f"[{i+1}] {title}\n"
            output += f"URL: {url}\n"
            output += f"Content: {content}\n\n"
            
        return output

    except Exception as e:
        return f"TOOL ERROR: {str(e)}"

@mcp.tool()
async def visit_page(url: str):
    """
    Visit a specific URL and return its raw content.
    """
    try:
        print(f"Visiting URL: {url}")
        
        # requests handles redirection by default
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        # Use BeautifulSoup to extract text
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Remove script and style elements
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
            
        # Get text and clean up whitespace
        text = soup.get_text(separator='\n')
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        clean_text = '\n'.join(chunk for chunk in chunks if chunk)
        
        return f"=== CONTENT OF {url} ===\n\n{clean_text}"

    except Exception as e:
        return f"TOOL ERROR: {str(e)}"

if __name__ == "__main__":
    mcp.run(transport="sse")