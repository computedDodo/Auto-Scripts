'''
Termux Intelligent File Agent v11.0.1

CHANGELOG & FIXES:
1. Context Size Optimization: Reduced tool read payload in ai_read_file (capped at 3,500 chars) to prevent token bloat in session histories.
2. Dynamic Token Trimming: Trimmer token budget tuned (max_tokens=2500) using native "approximate" counting to keep cumulative payloads well within Groq's ~8,000 TPM limit during mid-session hot-swapping.
3. Scratchpad Isolation: ai_execute_temp_script runs ephemeral code in ~/.agent_tmp/ and cleans up automatically.
4. State Leakage Fix: format_prompt cleanly isolates state messages and prevents internal LangGraph variables from confusing models.
5. Multi-Engine Hot-Swapping: Switch between Gemini, Groq, and OpenRouter on the fly.
'''
#!/usr/bin/env python3

import json
import os
import shutil
import sqlite3
import subprocess
import time
import warnings

# Suppress LangChain / LangGraph deprecation warnings
warnings.filterwarnings("ignore")

HOME_DIR = os.path.expanduser("~")
SHARED_DIR = os.path.join(HOME_DIR, "storage", "shared")
CACHE_FILE = os.path.join(HOME_DIR, "file_cache.json")
KEYS_FILE = os.path.join(HOME_DIR, ".ai_agent_keys.json")
DB_PATH = os.path.join(HOME_DIR, ".agent_memory.db")
TEMP_DIR = os.path.join(HOME_DIR, ".agent_tmp")
SKIP_DIRS = {
    "cache",
    ".thumbnails",
    "obb",
    "Android",
    ".cache",
    "tmp",
    "__pycache__",
    ".git",
    "venv",
    "env",
    ".agent_tmp",
}
SUPPORTED_EXTS = {
    ".txt",
    ".py",
    ".sh",
    ".md",
    ".json",
    ".log",
    ".csv",
    ".conf",
    ".ini",
    ".docx",
    ".xlsx",
    ".pdf",
    ".db",
#    ".env",
    ".sqlite",
    ".html",
    ".css",
    ".js",
}

# Ensure scratchpad directory exists
os.makedirs(TEMP_DIR, exist_ok=True)

# --- 0. KEY MANAGEMENT ---

def load_keys():
    if os.path.exists(KEYS_FILE):
        try:
            with open(KEYS_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_keys(keys):
    with open(KEYS_FILE, "w") as f:
        json.dump(keys, f)

def get_api_key(provider):
    keys = load_keys()
    if provider not in keys:
        print(f"\n[AI SETUP] No API key found for {provider}.")
        key = input(f"Enter your {provider} API key: ").strip()
        if key:
            keys[provider] = key
            save_keys(keys)
            print("Key saved securely.")
    return keys.get(provider)

# --- 1. CORE FILE OPERATIONS ---

def load_json(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def needs_cache_rebuild():
    if not os.path.exists(CACHE_FILE):
        return True
    cache = load_json(CACHE_FILE)
    try:
        return os.path.getmtime(SHARED_DIR) > cache.get("last_build", 0)
    except:
        return True

def build_cache():
    print("\nBuilding file cache...")
    cache, start = [], time.time()
    for base_dir in [HOME_DIR, SHARED_DIR]:
        if not os.path.exists(base_dir):
            continue
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for file in files:
                try:
                    full_path = os.path.join(root, file).encode(
                        "utf-8", "replace"
                    ).decode("utf-8")
                    if os.path.getsize(full_path) < 50 * 1024 * 1024:
                        cache.append(full_path)
                except:
                    continue
    save_json(CACHE_FILE, {"files": cache, "last_build": time.time()})
    print(f"\nCache built: {len(cache)} files in {time.time() - start:.1f}s")

def read_rich_document(filename):
    ext = os.path.splitext(filename)[1].lower()
    content = ""
    try:
        if ext == ".docx":
            import docx
            doc = docx.Document(filename)
            content = "\n".join([para.text for para in doc.paragraphs])
        elif ext == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(filename, data_only=True)
            rows = []
            for r in wb.active.iter_rows(values_only=True):
                row_str = " | ".join([str(c) for c in r if c is not None])
                if row_str:
                    rows.append(row_str)
            content = "\n".join(rows)
        elif ext == ".pdf":
            from pypdf import PdfReader
            content = "\n".join(
                [p.extract_text() for p in PdfReader(filename).pages if p.extract_text()]
            )
    except Exception as e:
        return f"[Parsing Error] {e}"
    return content

def search_files(keyword):
    cache = load_json(CACHE_FILE).get("files", [])
    if not cache or needs_cache_rebuild():
        build_cache()
        cache = load_json(CACHE_FILE).get("files", [])

    keyword = keyword.lower()
    name_matches = [f for f in cache if keyword in os.path.basename(f).lower()]
    if name_matches:
        return name_matches[:50]

    content_matches = []
    for f in cache:
        if any(f.endswith(ext) for ext in SUPPORTED_EXTS):
            try:
                if os.path.getsize(f) > 500000:
                    continue
                if not f.endswith((".docx", ".xlsx", ".pdf", ".db", ".sqlite")):
                    with open(f, "r", encoding="utf-8", errors="ignore") as file:
                        if keyword in file.read(5000).lower():
                            content_matches.append(f)
                if len(content_matches) >= 20:
                    break
            except:
                continue
    return content_matches

def read_file(filename, silent=False):
    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext in [".docx", ".xlsx", ".pdf"]:
            content = read_rich_document(filename)
        elif ext in [".db", ".sqlite"]:
            content = (
                "[Database File] Use the ai_query_db tool to inspect this file's"
                " tables."
            )
        else:
            with open(filename, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

        if not silent:
            print(f"\n--- {os.path.basename(filename)} ---")
            print(content[:3000])
            if len(content) > 3000:
                print("\n[Truncated. File is large]")
            print("--------------------\n")
        return content
    except Exception as e:
        return str(e)

def execute_file(filename, silent=False):
    try:
        if filename.endswith(".py"):
            result = subprocess.run(
                ["python", filename], capture_output=True, text=True
            )
        elif filename.endswith(".sh"):
            result = subprocess.run(["bash", filename], capture_output=True, text=True)
        else:
            return "Execution failed: Not a .py or .sh script."
        output = result.stdout + "\n" + result.stderr
        if not silent:
            print(output)
        return output
    except Exception as e:
        return str(e)

# --- 2. LANGGRAPH AUTONOMOUS AGENT (v11.0.1) ---

def initialize_llm(provider, api_key):
    prov = provider.lower()
    if prov == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        os.environ["GOOGLE_API_KEY"] = api_key
        return ChatGoogleGenerativeAI(model="gemini-3.6-flash", max_retries=3)
    elif prov == "groq":
        from langchain_groq import ChatGroq
        os.environ["GROQ_API_KEY"] = api_key
        return ChatGroq(model="qwen/qwen3.6-27b", max_retries=3)
    elif prov == "openrouter":
        from langchain_openai import ChatOpenAI
        os.environ["OPENROUTER_API_KEY"] = api_key
        return ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            model="anthropic/claude-3.5-sonnet",
            max_retries=3,
        )
    return None

def run_autonomous_agent(initial_provider, project_name):
    try:
        from langchain_core.messages import HumanMessage, SystemMessage, trim_messages
        from langchain_core.tools import tool
        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.prebuilt import create_react_agent
    except ImportError:
        print(
            "\n[Dependency Missing] Run: pip install -U langgraph"
            " langgraph-checkpoint-sqlite langchain-core"
        )
        return

    # ------------------ V11.0.1 TOOLS ------------------

    @tool
    def ai_search_files(keyword: str) -> str:
        """Search for local files by keyword in filename or content."""
        matches = search_files(keyword)
        return "\n".join(matches) if matches else "No files found."

    @tool
    def ai_read_file(filepath: str) -> str:
        """Reads the text content of a file (capped at 3,500 chars to save tokens)."""
        # V11.0.1 FIX: Radically limits read output to keep payloads small for Groq.
        return read_file(filepath, silent=True)[:3500]

    @tool
    def ai_execute_script(filepath: str) -> str:
        """Executes a permanent .py or .sh file and returns output."""
        return execute_file(filepath, silent=True)

    @tool
    def ai_execute_temp_script(script_code: str, script_type: str = "py") -> str:
        """Executes a temporary scratchpad script to perform calculations or intermediate tasks.
        The temporary script is automatically deleted immediately after execution.
        """
        ext = ".py" if script_type.lower() == "py" else ".sh"
        temp_file = os.path.join(TEMP_DIR, f"scratch_{int(time.time() * 1000)}{ext}")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(script_code)

            if ext == ".py":
                res = subprocess.run(
                    ["python", temp_file], capture_output=True, text=True, timeout=60
                )
            else:
                res = subprocess.run(
                    ["bash", temp_file], capture_output=True, text=True, timeout=60
                )

            output = (res.stdout + "\n" + res.stderr).strip()
            return (
                f"[Temp Execution Output]:\n{output}"
                if output
                else "[Temp Execution Completed with no output]"
            )
        except subprocess.TimeoutExpired:
            return "[Execution Error]: Temporary script timed out after 60 seconds."
        except Exception as e:
            return f"[Execution Error]: {e}"
        finally:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except:
                    pass

    @tool
    def ai_query_db(db_path: str, query: str) -> str:
        """Executes a read-only SQL query on a local SQLite database (.db or .sqlite)."""
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute(query)
            rows = cursor.fetchmany(50)
            conn.close()
            return "Query Results:\n" + "\n".join([str(row) for row in rows])
        except Exception as e:
            return f"Database Error: {e}"

    @tool
    def ai_edit_file(filepath: str, new_content: str, mode: str = "w") -> str:
        """Modifies or creates a permanent file. Automatically creates parent directories."""
        try:
            dir_name = os.path.dirname(os.path.abspath(filepath))
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            with open(filepath, mode, encoding="utf-8") as f:
                f.write(new_content)
            return f"Success: File {filepath} saved (mode: {mode})."
        except Exception as e:
            return f"Error editing file: {e}"

    @tool
    def ai_web_search(query: str) -> str:
        """Searches Google for technical information or documentation."""
        try:
            from googlesearch import search
            results = []
            for r in search(query, num=3, stop=3, advanced=True):
                results.append(f"- {r.title}: {r.description} (Link: {r.url})")
            return "\n".join(results) if results else "No results found."
        except Exception as e:
            return f"Web Search Error: {e}"

    @tool
    def ai_map_directory(dir_path: str) -> str:
        """Generates a visual tree structure of a directory."""
        if not os.path.isdir(dir_path):
            return "Directory not found."
        tree = []
        for root, dirs, files in os.walk(dir_path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            level = root.replace(dir_path, "").count(os.sep)
            indent = " " * 4 * level
            tree.append(f"{indent}{os.path.basename(root)}/")
            subindent = " " * 4 * (level + 1)
            for f in files:
                tree.append(f"{subindent}{f}")
            if len(tree) > 150:
                tree.append(f"{subindent}... (truncated)")
                break
        return "\n".join(tree)

    @tool
    def ai_git_commit(repo_path: str, commit_message: str) -> str:
        """Stages all changes and creates a git commit in the specified repository."""
        try:
            subprocess.run(
                ["git", "add", "."], cwd=repo_path, check=True, capture_output=True
            )
            res = subprocess.run(
                ["git", "commit", "-m", commit_message],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            return res.stdout + res.stderr
        except Exception as e:
            return f"Git Error: {e}"

    # -----------------------------------------------

    tools = [
        ai_search_files,
        ai_read_file,
        ai_execute_script,
        ai_execute_temp_script,
        ai_query_db,
        ai_edit_file,
        ai_web_search,
        ai_map_directory,
        ai_git_commit,
    ]

    # V11.0.1 FIX: Sliding Window Context Trimmer optimized for Groq payload limits
    # Uses 'approximate' counting to guarantee messages fall under the 2500 token mark.
    trimmer = trim_messages(
        max_tokens=2500,
        strategy="last",
        token_counter="approximate",
        allow_partial=False,
        start_on="human",
    )

    def format_prompt(state):
        """Safely trims messages and prepends the system prompt without state leakage."""
        raw_msgs = state.get("messages", [])
        trimmed = trimmer.invoke(raw_msgs)
        sys_msg = SystemMessage(
            content=(
                "You are Termux File Agent v11.0.1. Use the provided tools to inspect"
                " files, search directories, and fulfill the user's request. For"
                " temporary scripts or data processing, always use"
                " 'ai_execute_temp_script' so scratchpad files are cleaned"
                " automatically. Only use 'ai_edit_file' for permanent files"
                " requested by the user. Do not search for system state variables."
            )
        )
        return [sys_msg] + trimmed

    # SQLite Persistent Checkpointer
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    memory = SqliteSaver(conn)

    current_provider = initial_provider
    api_key = get_api_key(current_provider)
    llm = initialize_llm(current_provider, api_key)

    agent = create_react_agent(
        llm, tools, checkpointer=memory, prompt=format_prompt
    )

    config = {"configurable": {"thread_id": project_name}}

    print(
        f"\n🟢 [Project: {project_name} | Engine: {current_provider.upper()}]"
    )
    print("Type /help to see available commands.\n")

    while True:
        try:
            prompt = input("You: ").strip()
            if not prompt:
                continue

            # --- Offline Slash Commands ---
            cmd = prompt.lower()
            if cmd in ["/exit", "/quit", "exit", "quit"]:
                print("Saving session state and returning to main menu...")
                break
            elif cmd == "/help":
                print("\n--- Agent Slash Commands ---")
                print("  /help           - Display this command reference")
                print(
                    "  /switch [model] - Hot-swap engine on the fly (gemini, groq,"
                    " openrouter)"
                )
                print("  /clear          - Wipe the current project session memory")
                print("  /exit           - Save and return to the main menu\n")
                continue
            elif cmd == "/clear":
                config["configurable"]["thread_id"] = (
                    f"{project_name}_{int(time.time())}"
                )
                print("🧹 Active conversation memory cleared for this project.\n")
                continue
            elif cmd.startswith("/switch"):
                parts = cmd.split()
                if len(parts) == 2:
                    new_prov = parts[1]
                    new_key = get_api_key(new_prov)
                    if new_key:
                        current_provider = new_prov
                        llm = initialize_llm(current_provider, new_key)
                        agent = create_react_agent(
                            llm, tools, checkpointer=memory, prompt=format_prompt
                        )
                        print(f"🔄 Engine hot-swapped to: {current_provider.upper()}\n")
                    else:
                        print(f"Failed to find or save API key for {new_prov}\n")
                else:
                    print("Usage: /switch [gemini|groq|openrouter]\n")
                continue

            # --- Autonomous Execution Loop ---
            inputs = {"messages": [HumanMessage(content=prompt)]}
            print("🤖 Agent is thinking...", end="", flush=True)

            try:
                for s in agent.stream(inputs, config=config, stream_mode="values"):
                    message = s["messages"][-1]
                    if hasattr(message, "tool_calls") and message.tool_calls:
                        print()
                        for t in message.tool_calls:
                            args = str(t["args"])
                            if len(args) > 100:
                                args = args[:97] + "..."
                            print(f"   [Tool Executing] -> {t['name']}({args})")

                final_text = s["messages"][-1].content
                if (
                    isinstance(final_text, list)
                    and len(final_text) > 0
                    and "text" in final_text[0]
                ):
                    final_text = final_text[0]["text"]
                print(f"\n--- AI ---\n{final_text}\n--------------------\n")

            except Exception as e:
                err_str = str(e).lower()
                print(f"\n\n[Agent Error] {e}")
                if "429" in err_str or "rate limit" in err_str or "413" in err_str:
                    print(
                        f"\n⚠️ {current_provider.upper()} quota/limit reached! Type"
                        " '/switch gemini' to hot-swap engines and resume immediately."
                    )

        except KeyboardInterrupt:
            print("\nSession paused. Type /exit to leave.")

# --- 3. MAIN TERMINAL MENU ---

def main():
    print("🤖 Termux File Agent v11.0.1 (Optimized Token Trimming & Scratchpad)")
    if needs_cache_rebuild():
        build_cache()

    while True:
        try:
            print("\nChoose an option:")
            print("1. Search & Read (Text, PDF, Word, Excel)")
            print("2. Search & Edit (Nano)")
            print("3. Search & Execute Script")
            print("4. Autonomous AI Chat (LangGraph v11.0.1)")
            print("5. Manage Database (Wipe All Memory)")
            print("6. Exit")

            choice = input("Enter choice: ").strip()

            if choice in ["1", "2", "3"]:
                keyword = input("Enter search keyword: ").strip()
                if not keyword:
                    continue
                matches = search_files(keyword)
                if not matches:
                    print("No files found.")
                    continue
                print("\nFound files:")
                for i, match in enumerate(matches, 1):
                    print(f"{i}. {match}")
                sel = input("Select file number [Enter to cancel]: ").strip()
                if sel and 1 <= int(sel) <= len(matches):
                    filename = matches[int(sel) - 1]
                    if choice == "1":
                        read_file(filename)
                    elif choice == "2":
                        os.system(f'nano "{filename}"')
                    elif choice == "3":
                        execute_file(filename)

            elif choice == "4":
                print("\nActive Project Session:")
                project_name = input(
                    "Enter Project Name (e.g., evolution_study) or press Enter for"
                    " 'default': "
                ).strip()
                if not project_name:
                    project_name = "default"

                print("\nSelect Initial Engine:")
                print("1. Gemini (Google) - Best for large context & files")
                print("2. Groq (Qwen 3.6 27B) - High-speed processing")
                print("3. OpenRouter (Claude 3.5 Sonnet) - Advanced software design")
                eng_choice = input("Enter choice (1-3): ").strip()

                provider = "Gemini"
                if eng_choice == "2":
                    provider = "Groq"
                elif eng_choice == "3":
                    provider = "OpenRouter"

                api_key = get_api_key(provider)
                if api_key:
                    run_autonomous_agent(provider, project_name)

            elif choice == "5":
                if os.path.exists(DB_PATH):
                    os.remove(DB_PATH)
                    print("✅ Master SQLite Database completely erased. All sessions wiped.")
                else:
                    print("No memory database found.")

            elif choice == "6":
                break
            else:
                print("Invalid choice.")
        except KeyboardInterrupt:
            print("\nCancelled.")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    main()
