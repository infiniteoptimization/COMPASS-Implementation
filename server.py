import os
import json
import uuid
import sqlite3
import asyncio
from datetime import datetime
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from dotenv import load_dotenv

from openai import OpenAI
from fastmcp import Client

load_dotenv()

# --- Configuration ---
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", 'https://openrouter.ai/api/v1')
OPENAI_API_KEY = os.getenv("OpenRouterAPIKey")
MCP_SERVER_URL = "http://localhost:8000/sse"
MODEL_NAME = "xiaomi/mimo-v2-flash:free"#"moonshotai/kimi-k2-0905" not work #"x-ai/grok-4.1-fast"  work  "deepseek/deepseek-v3.2" work special not work  "minimax/minimax-m2" work "z-ai/glm-4.6v" work
DB_FILE = "chat_history.db"

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Database Management (SQLite) ---

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions
                 (id TEXT PRIMARY KEY, title TEXT, created_at TEXT, agent_notes TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp TEXT)''')
    conn.commit()
    conn.close()

def get_sessions_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, title, created_at FROM sessions ORDER BY created_at DESC")
    sessions = [{"id": s[0], "title": s[1], "created_at": s[2]} for s in c.fetchall()]
    conn.close()
    return sessions

def create_session_db(first_message):
    session_id = str(uuid.uuid4())
    title = first_message[:30] + "..." if len(first_message) > 30 else first_message
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO sessions (id, title, created_at, agent_notes) VALUES (?, ?, ?, ?)",
              (session_id, title, datetime.now().isoformat(), "Initial State: Task just started."))
    conn.commit()
    conn.close()
    return session_id

def get_messages_db(session_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT role, content FROM messages WHERE session_id = ? ORDER BY id ASC", (session_id,))
    msgs = c.fetchall()
    conn.close()
    return [{"role": m[0], "content": m[1]} for m in msgs]

def save_message_db(session_id, role, content):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
              (session_id, role, content, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_agent_notes_db(session_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT agent_notes FROM sessions WHERE id = ?", (session_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else "Initial State: Task just started."

def update_agent_notes_db(session_id, new_notes):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE sessions SET agent_notes = ? WHERE id = ?", (new_notes, session_id))
    conn.commit()
    conn.close()

# Initialize DB on start
init_db()

# --- The Compass Agent System (Logic Preserved) ---

class CompassSystem:
    def __init__(self, mcp_session, current_notes, session_id, max_inner_steps=5, max_outer_loops=4):
        self.client = OpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)
        self.mcp_session = mcp_session
        self.notes = current_notes
        self.available_tools = [] 
        self.session_id = session_id
        self.max_inner_steps = max_inner_steps
        self.max_outer_loops = max_outer_loops
        self.log_file = os.path.join("logs", f"{session_id}.log")
        
        # Ensure log directory exists
        os.makedirs("logs", exist_ok=True)

    def _log_to_file(self, category, content):
        timestamp = datetime.now().isoformat()
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] [{category}]\n{content}\n{'-'*80}\n")
        except Exception as e:
            print(f"Failed to write to log file: {e}") 

    async def fetch_tools_for_openai(self):
        try:
            tools = await self.mcp_session.list_tools()
            # Handle both fastmcp (list) and standard mcp (object with .tools)
            if hasattr(tools, 'tools'):
                tools = tools.tools
                
            openai_tools = []
            for tool in tools:
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.inputSchema 
                    }
                })
            self.available_tools = openai_tools
            return openai_tools
        except Exception as e:
            print(f"Error fetching tools: {e}")
            return []

    def log_stream(self, loop_type, role, content):
        """Yields log messages."""
        display_content = (content[:500] + '...') if len(content) > 500 else content
        # Changed to yield JSON for frontend parsing
        return json.dumps({
            "type": "log",
            "loop_type": loop_type,
            "role": role,
            "content": display_content
        })

    async def call_mcp_tool(self, tool_name, arguments):
        self._log_to_file("MCP CALL", f"Function: {tool_name}\nArguments: {json.dumps(arguments, indent=2)}")
        try:
            result = await self.mcp_session.call_tool(name=tool_name, arguments=arguments)
            text_content = ""
            if hasattr(result, 'content'):
                for c in result.content:
                    if hasattr(c, 'text') and c.text:
                        text_content += c.text
                    elif hasattr(c, 'value'):
                        text_content += str(c.value)
                    else:
                        text_content += str(c)
            if not text_content:
                self._log_to_file("MCP RESULT", "Tool executed but returned no content.")
                return "Tool executed but returned no content."
            self._log_to_file("MCP RESULT", text_content)
            return text_content
        except Exception as e:
            return f"Error calling {tool_name}: {str(e)}"

    def synthesize_context_for_agent(self, query, meta_signal=None):
        prompt = (
            "You are the Context Manager. Synthesize a concise, execution-ready context for the Main Agent.\n"
            "Focus strictly on the IMMEDIATE next steps based on the strategic signal.\n\n"
            f"--- ORIGINAL QUERY ---\n{query}\n\n"
            f"--- ACCUMULATED NOTES ---\n{self.notes}\n\n"
        )
        if meta_signal:
            prompt += f"--- STRATEGIC INTERVENTION (FROM META-THINKER) ---\n{meta_signal}\n(This is the highest priority instruction!)\n\n"
        prompt += (
            "Output a Structured Context containing:\n"
            "1. Task: One sentence restatement.\n"
            "2. Verified Evidence: Bullet points of confirmed facts.\n"
            "3. Constraints: Specific rules.\n"
            "4. Next Plan: 2-3 concrete steps for the Main Agent to execute NOW."
        )
        self._log_to_file("AGENT PROMPT (Context Manager)", prompt)
        response = self.client.chat.completions.create(
            model=MODEL_NAME, messages=[{"role": "user", "content": prompt}]
        )
        content = response.choices[0].message.content
        self._log_to_file("AGENT OUTPUT (Context Manager)", content)
        return content

    def update_notes_logic(self, query, trajectory, meta_data):
        prompt = (
            "You are the Context Manager. Update the global Research Notes.\n"
            "CRITICAL INSTRUCTION: If the Recent Trajectory contains the final answers, "
            "extract them into 'Verified Evidence'.\n\n"
            f"--- ORIGINAL QUERY ---\n{query}\n\n"
            f"--- OLD NOTES ---\n{self.notes}\n\n"
            f"--- RECENT TRAJECTORY ---\n{trajectory}\n\n"
            f"--- META-THINKER JUDGMENT ---\nDecision: {meta_data.get('decision')}\nReason for Interruption: {meta_data.get('reason','No reason provided')}\nStrategic Signal: {meta_data.get('strategic_signal','No strategic signal provided')}\n"
            "Return ONLY the updated text of the Notes."
        )
        self._log_to_file("AGENT PROMPT (Context Manager - Update Notes)", prompt)
        response = self.client.chat.completions.create(model=MODEL_NAME, messages=[{"role": "user", "content": prompt}])
        self.notes = response.choices[0].message.content
        self._log_to_file("AGENT OUTPUT (Context Manager - Update Notes)", self.notes)
        return self.notes

    def meta_think(self, current_brief, current_trajectory):
        prompt = (
            "You are the Meta-Thinker. Decide execution flow.\n"
            f"--- CURRENT CONTEXT ---\n{current_brief}\n\n"
            f"--- RECENT TRAJECTORY ---\n{current_trajectory}\n\n"
            "Return JSON: { 'decision': 'CONTINUE'|'INTERRUPT'|'COMPLETED', 'reason': '...', 'strategic_signal': '...' }"
        )
        self._log_to_file("AGENT PROMPT (Meta-Thinker)", prompt)
        try:
            response = self.client.chat.completions.create(
                model=MODEL_NAME, 
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            content = response.choices[0].message.content
            self._log_to_file("AGENT OUTPUT (Meta-Thinker)", content)
            return json.loads(content)
        except:
            return {"decision": "INTERRUPT", "reason": "JSON Error", "strategic_signal": "Error parsing meta response."}

    def extract_final_answer(self, query):
        prompt = (
            "You are the Answer Synthesizer. Formulate a final answer based on notes.\n"
            f"--- USER QUERY ---\n{query}\n\n"
            f"--- VERIFIED RESEARCH NOTES ---\n{self.notes}\n"
        )
        self._log_to_file("AGENT PROMPT (Answer Synthesizer)", prompt)
        response = self.client.chat.completions.create(model=MODEL_NAME, messages=[{"role": "user", "content": prompt}])
        content = response.choices[0].message.content
        self._log_to_file("AGENT OUTPUT (Answer Synthesizer)", content)
        return content

    async def execute_inner_loop(self, context_brief):
        messages = [
            {"role": "system", "content": (
                "You are the Main Agent (Tactical). Execute the plan provided in the Context.\n"
                "Use the provided tools to gather information.\n"
                "Focus ONLY on the immediate 'Next Plan'."
            )},
            {"role": "user", "content": f"--- STARTING CONTEXT ---\n{context_brief}"}
        ]
        
        trajectory_log = ""
        tools_schema = self.available_tools if self.available_tools else None

        for i in range(self.max_inner_steps):
            yield self.log_stream("INNER", f"Main Agent (Step {i+1})", "Thinking...")
            
            # --- FIX STARTS HERE ---
            # Helper to sanitize messages for logging (converts Objects to Dicts)
            serializable_msgs = []
            for m in messages:
                if isinstance(m, dict):
                    serializable_msgs.append(m)
                elif hasattr(m, 'model_dump'): # OpenAI V1+ objects
                    serializable_msgs.append(m.model_dump())
                elif hasattr(m, 'to_dict'): # Older versions
                    serializable_msgs.append(m.to_dict())
                else:
                    serializable_msgs.append(str(m))
            
            self._log_to_file("AGENT PROMPT (Main Agent)", json.dumps(serializable_msgs, indent=2))
            
            if tools_schema:
                response = self.client.chat.completions.create(
                    model=MODEL_NAME, messages=messages, tools=tools_schema
                )
            else:
                response = self.client.chat.completions.create(
                    model=MODEL_NAME, messages=messages
                )
            
            self._log_to_file("AGENT OUTPUT (Main Agent)", response.choices[0].message.content or "Tool Call")

            msg = response.choices[0].message
            messages.append(msg)
            
            step_desc = ""
            
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments)
                    tool_name = tc.function.name
                    step_desc += f"Action: {tool_name}({args})\n"
                    yield self.log_stream("INNER", "Tool Call", f"{tool_name}: {args}")
                    
                    res = await self.call_mcp_tool(tool_name, args)
                    short_res = (res[:200] + '...') if len(res) > 200 else res
                    yield self.log_stream("INNER", "Tool Result", f"{short_res}")
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": res})
                    step_desc += f"Observation: {short_res}\n"
            else:
                step_desc += f"Thought: {msg.content}\n"
                yield self.log_stream("INNER", "Thought", msg.content)
            
            trajectory_log += f"Step {i+1}: {step_desc}\n"
            meta_data = self.meta_think(context_brief, trajectory_log)
            decision = meta_data.get('decision', 'CONTINUE')
            signal = meta_data.get('strategic_signal', 'None')
            yield self.log_stream("INNER", "Meta-Thinker", f"**{decision}**: {signal}")

            if decision in ['INTERRUPT', 'COMPLETED']:
                yield (trajectory_log, meta_data)
                return

        yield (trajectory_log, {"decision": "INTERRUPT", "strategic_signal": "Max steps reached.", "reason": "Timeout"})

    async def solve(self, query):
        yield self.log_stream("SYSTEM", "Start", f"Initializing COMPASS for: {query}")
        yield self.log_stream("SYSTEM", "Setup", "Discovering MCP Tools...")
        tools = await self.fetch_tools_for_openai()
        yield self.log_stream("SYSTEM", "Setup", f"Found {len(tools)} tools")

        last_meta_signal = None

        for t in range(self.max_outer_loops):
            yield self.log_stream("OUTER", f"Loop {t+1}", "Synthesizing Context...")
            current_context = self.synthesize_context_for_agent(query, last_meta_signal)
            yield self.log_stream("OUTER", "Context Brief", current_context)

            trajectory = ""
            meta_data = {}
            
            async for log_entry in self.execute_inner_loop(current_context):
                if isinstance(log_entry, tuple): 
                    trajectory, meta_data = log_entry
                else:
                    yield log_entry

            yield self.log_stream("OUTER", "Memory Update", f"Integrating findings... Decision: {meta_data.get('decision')}")
            self.update_notes_logic(query, trajectory, meta_data)
            
            if meta_data.get('decision') == 'COMPLETED':
                yield self.log_stream("SYSTEM", "Completion", "Generating final answer...")
                final_answer = self.extract_final_answer(query)
                yield json.dumps({
                    "type": "final_answer",
                    "content": final_answer
                })
                return
            
            last_meta_signal = meta_data.get('strategic_signal')

        final_msg = f"### Task Ended\nReached max loops.\n\n**Final Notes:**\n{self.notes}"
        yield json.dumps({
            "type": "final_answer",
            "content": final_msg
        })

# --- API Routes ---

@app.get("/api/sessions")
async def get_sessions_route():
    return get_sessions_db()

@app.post("/api/sessions")
async def create_session_route(request: Request):
    data = await request.json()
    first_msg = data.get("message", "New Chat")
    session_id = create_session_db(first_msg)
    # Save the initial user message
    save_message_db(session_id, "user", first_msg)
    return {"id": session_id}

@app.get("/api/sessions/{session_id}/messages")
async def get_messages_route(session_id: str):
    return get_messages_db(session_id)

@app.get("/api/chat_stream")
async def chat_stream(session_id: str, query: str):
    """
    SSE Endpoint for running the agent.
    """
    async def event_generator():
        # 1. Retrieve notes
        current_notes = get_agent_notes_db(session_id)
        
        # 2. Connect to MCP
        try:
            async with Client(MCP_SERVER_URL) as mcp_client:
                agent = CompassSystem(mcp_client, current_notes, session_id)
                
                # 3. Run Agent Loop

                async for update_json in agent.solve(query):
                    yield {"data": update_json}
                    
                    # Check if it's the final answer to save it
                    data = json.loads(update_json)
                    if data["type"] == "final_answer":
                        final_answer = data["content"]
                
                # 4. Update DB
                update_agent_notes_db(session_id, agent.notes)
                save_message_db(session_id, "assistant", final_answer)
                    
        except Exception as e:
            err_msg = json.dumps({"type": "error", "content": str(e)})
            yield {"data": err_msg}

    return EventSourceResponse(event_generator())

# Mount static files (Frontend)
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8501, reload=True)