import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
import sys
import asyncio
import base64
from typing import Optional, Dict, Any, List, Tuple
from github import Github, GithubException
from dotenv import load_dotenv
import re
from datetime import datetime
import aiosqlite
import psycopg2
from contextlib import closing
import hashlib

# ========== DISABLE VOICE SUPPORT ==========
import discord.voice_client
discord.voice_client.VoiceClient = None

# Load environment variables
load_dotenv()

# Configuration
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME")
DEFAULT_REPO = os.getenv("DEFAULT_REPO", "discord-projects")
DATABASE_URL = os.getenv("DATABASE_URL")

# Validate required environment variables
if not DISCORD_TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable not set")
    sys.exit(1)
if not GITHUB_TOKEN:
    print("ERROR: GITHUB_TOKEN environment variable not set")
    sys.exit(1)
if not GITHUB_USERNAME:
    print("ERROR: GITHUB_USERNAME environment variable not set")
    sys.exit(1)

# Initialize bot
DEFAULT_PREFIX = "--"
intents = discord.Intents.default()
intents.message_content = True

class GitCordBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=self.get_prefix,
            intents=intents,
            help_command=None
        )
        self.db = None
        self.github_client = None
        self.github_username = GITHUB_USERNAME
        self.startup_time = datetime.now()
        self.github_handler = None
    
    async def get_prefix(self, message):
        """Dynamic prefix based on user settings"""
        if not self.db:
            return DEFAULT_PREFIX
        
        try:
            prefix = await self.db.get_user_prefix(message.author.id)
            return prefix or DEFAULT_PREFIX
        except:
            return DEFAULT_PREFIX
    
    async def setup_hook(self):
        # Initialize handlers first
        await self.initialize_systems()
        
        # Then add cogs
        await self.add_cog(GitHubCommands(self))
        await self.add_cog(AdminCommands(self))
        await self.add_cog(UtilityCommands(self))
        await self.tree.sync()
        
    async def on_ready(self):
        print(f'Bot {self.user} is online')
        print(f'Default Prefix: {DEFAULT_PREFIX}')
        print(f'GitHub User: {self.github_username}')
        
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="GitHub repositories"
        ))

    async def initialize_systems(self):
        """Initialize all systems"""
        # Database
        self.db = DatabaseManager()
        if await self.db.initialize():
            print('Database connected')
        else:
            print('Database not available')
            return False
        
        # GitHub client
        try:
            self.github_client = Github(GITHUB_TOKEN)
            user = self.github_client.get_user()
            actual_username = user.login
            print(f'GitHub authenticated as {actual_username}')
            
            if actual_username.lower() != self.github_username.lower():
                print(f'Username mismatch: ENV={self.github_username}, TOKEN={actual_username}')
                print(f'Using: {actual_username}')
                self.github_username = actual_username
            
            rate_limit = self.github_client.get_rate_limit().core
            print(f'GitHub Rate Limit: {rate_limit.remaining}/{rate_limit.limit}')
            
            # Initialize GitHub handler
            self.github_handler = GitHubHandler(self.github_client, self.db, self.github_username)
            
        except GithubException as e:
            print(f'GitHub API Error: {e.status} - {e.data.get("message", "Unknown error")}')
            return False
        except Exception as e:
            print(f'GitHub authentication failed: {e}')
            return False
        
        return True

bot = GitCordBot()

# ========== DATABASE MANAGER ==========

class DatabaseManager:
    def __init__(self):
        self.db_type = None
        self.sqlite_conn = None
        self.pg_conn = None
        
    async def initialize(self):
        if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
            try:
                self.pg_conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
                self.db_type = "postgresql"
                await self._create_postgres_tables()
                print('PostgreSQL connected')
                return True
            except Exception as e:
                print(f'PostgreSQL failed: {e}')
        
        # Fallback to SQLite
        try:
            self.sqlite_conn = await aiosqlite.connect("gitcord.db")
            self.db_type = "sqlite"
            await self._create_sqlite_tables()
            print('SQLite connected')
            return True
        except Exception as e:
            print(f'SQLite failed: {e}')
            return False
    
    async def _create_postgres_tables(self):
        with closing(self.pg_conn.cursor()) as cur:
            tables = [
                '''CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY,
                    default_repo TEXT,
                    current_branch TEXT DEFAULT 'main',
                    commit_message TEXT DEFAULT 'Update via GitCord',
                    preferred_prefix TEXT DEFAULT '--',
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )''',
                
                '''CREATE TABLE IF NOT EXISTS command_logs (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    command TEXT,
                    arguments TEXT,
                    success BOOLEAN,
                    error_message TEXT,
                    execution_time FLOAT,
                    created_at TIMESTAMP DEFAULT NOW()
                )''',
                
                '''CREATE TABLE IF NOT EXISTS file_history (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    repo_name TEXT,
                    filename TEXT,
                    action TEXT,
                    content_hash TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )'''
            ]
            
            for table_sql in tables:
                cur.execute(table_sql)
            
            self.pg_conn.commit()
    
    async def _create_sqlite_tables(self):
        await self.sqlite_conn.execute("PRAGMA foreign_keys = ON")
        
        tables = [
            '''CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                default_repo TEXT,
                current_branch TEXT DEFAULT 'main',
                commit_message TEXT DEFAULT 'Update via GitCord',
                preferred_prefix TEXT DEFAULT '--',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''',
            
            '''CREATE TABLE IF NOT EXISTS command_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                command TEXT,
                arguments TEXT,
                success BOOLEAN,
                error_message TEXT,
                execution_time REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''',
            
            '''CREATE TABLE IF NOT EXISTS file_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                repo_name TEXT,
                filename TEXT,
                action TEXT,
                content_hash TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        ]
        
        for table_sql in tables:
            await self.sqlite_conn.execute(table_sql)
        
        await self.sqlite_conn.commit()
    
    async def execute(self, query: str, *args):
        try:
            if self.db_type == "postgresql":
                with closing(self.pg_conn.cursor()) as cur:
                    cur.execute(query, args)
                    self.pg_conn.commit()
            elif self.db_type == "sqlite":
                await self.sqlite_conn.execute(query, args)
                await self.sqlite_conn.commit()
        except Exception as e:
            print(f"Database execute error: {e}")
    
    async def fetchrow(self, query: str, *args):
        try:
            if self.db_type == "postgresql":
                with closing(self.pg_conn.cursor()) as cur:
                    cur.execute(query, args)
                    row = cur.fetchone()
                    if row and cur.description:
                        return dict(zip([desc[0] for desc in cur.description], row))
                    return None
            elif self.db_type == "sqlite":
                cursor = await self.sqlite_conn.execute(query, args)
                row = await cursor.fetchone()
                await cursor.close()
                if row:
                    return dict(row)
                return None
        except Exception as e:
            print(f"Database fetchrow error: {e}")
            return None
    
    async def fetch(self, query: str, *args):
        try:
            if self.db_type == "postgresql":
                with closing(self.pg_conn.cursor()) as cur:
                    cur.execute(query, args)
                    rows = cur.fetchall()
                    if cur.description:
                        columns = [desc[0] for desc in cur.description]
                        return [dict(zip(columns, row)) for row in rows]
                    return []
            elif self.db_type == "sqlite":
                cursor = await self.sqlite_conn.execute(query, args)
                rows = await cursor.fetchall()
                await cursor.close()
                if rows:
                    return [dict(row) for row in rows]
                return []
        except Exception as e:
            print(f"Database fetch error: {e}")
            return []
    
    async def get_user_settings(self, user_id: int) -> Dict[str, Any]:
        """Get all user settings - ALWAYS fresh from database"""
        query = 'SELECT * FROM user_settings WHERE user_id = $1'
        return await self.fetchrow(query, user_id) or {}
    
    async def update_user_settings(self, user_id: int, **kwargs):
        """Update user settings"""
        settings = await self.get_user_settings(user_id)
        
        if settings:
            set_clause = ', '.join([f"{k} = ${i+2}" for i, k in enumerate(kwargs.keys())])
            values = [user_id] + list(kwargs.values())
            query = f'UPDATE user_settings SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE user_id = $1'
            await self.execute(query, *values)
        else:
            columns = ['user_id'] + list(kwargs.keys())
            placeholders = ', '.join([f'${i+1}' for i in range(len(columns))])
            values = [user_id] + list(kwargs.values())
            query = f'INSERT INTO user_settings ({", ".join(columns)}) VALUES ({placeholders})'
            await self.execute(query, *values)
    
    async def get_user_repo(self, user_id: int) -> str:
        """Get current repository for user - ALWAYS fresh from database"""
        settings = await self.get_user_settings(user_id)
        repo = settings.get('default_repo', DEFAULT_REPO)
        return repo
    
    async def set_user_repo(self, user_id: int, repo_name: str):
        """Set current repository for user"""
        await self.update_user_settings(user_id, default_repo=repo_name)
    
    async def get_user_branch(self, user_id: int) -> str:
        """Get current branch for user - ALWAYS fresh from database"""
        settings = await self.get_user_settings(user_id)
        return settings.get('current_branch', 'main')
    
    async def set_user_branch(self, user_id: int, branch_name: str):
        """Set current branch for user"""
        await self.update_user_settings(user_id, current_branch=branch_name)
    
    async def get_user_commit_message(self, user_id: int) -> str:
        """Get commit message for user - ALWAYS fresh from database"""
        settings = await self.get_user_settings(user_id)
        return settings.get('commit_message', 'Update via GitCord')
    
    async def set_user_commit_message(self, user_id: int, message: str):
        """Set commit message for user"""
        await self.update_user_settings(user_id, commit_message=message)
    
    async def get_user_prefix(self, user_id: int) -> str:
        """Get command prefix for user - ALWAYS fresh from database"""
        settings = await self.get_user_settings(user_id)
        return settings.get('preferred_prefix', DEFAULT_PREFIX)
    
    async def set_user_prefix(self, user_id: int, prefix: str):
        """Set command prefix for user"""
        await self.update_user_settings(user_id, preferred_prefix=prefix)
    
    async def log_command(self, user_id: int, command: str, arguments: str, success: bool, 
                         error_message: str = None, execution_time: float = 0.0):
        """Log command execution"""
        await self.execute(
            'INSERT INTO command_logs (user_id, command, arguments, success, error_message, execution_time) VALUES ($1, $2, $3, $4, $5, $6)',
            user_id, command, arguments, success, error_message, execution_time
        )
    
    async def close(self):
        try:
            if self.db_type == "sqlite" and self.sqlite_conn:
                await self.sqlite_conn.close()
            elif self.db_type == "postgresql" and self.pg_conn:
                self.pg_conn.close()
        except Exception as e:
            print(f"Database close error: {e}")

# ========== GITHUB HANDLER ==========

class GitHubHandler:
    """Centralized GitHub operations handler"""
    
    def __init__(self, github_client: Github, db: DatabaseManager, github_username: str):
        self.github_client = github_client
        self.db = db
        self.github_username = github_username
    
    async def get_user_context(self, user_id: int) -> Tuple[str, str, str]:
        """
        Get user's current repo, branch, and commit message
        ALWAYS fetches fresh from database - NO CACHING
        """
        repo = await self.db.get_user_repo(user_id)
        branch = await self.db.get_user_branch(user_id)
        commit_msg = await self.db.get_user_commit_message(user_id)
        return repo, branch, commit_msg
    
    async def validate_user_context(self, user_id: int) -> Tuple[bool, str]:
        """Validate user has proper context set up"""
        repo, branch, _ = await self.get_user_context(user_id)
        
        if not repo or repo == DEFAULT_REPO:
            return False, "Please select a repository first using `--repo <name>`"
        
        return True, ""
    
    def parse_repo_full_name(self, repo_full_name: str) -> Tuple[str, str]:
        """Parse owner and repo name from full name"""
        if '/' in repo_full_name:
            owner, repo_name = repo_full_name.split('/', 1)
        else:
            owner = self.github_username
            repo_name = repo_full_name
        return owner, repo_name
    
    async def api_request(self, method: str, endpoint: str, data: dict = None):
        """Make GitHub API request with proper error handling"""
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "GitCord-Bot"
        }
        
        url = f"https://api.github.com{endpoint}"
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.request(method, url, headers=headers, json=data, timeout=30) as response:
                    if response.status == 204:
                        return response.status, {}
                    response_data = await response.json() if response.content_length else {}
                    return response.status, response_data
            except asyncio.TimeoutError:
                return 408, {"message": "Request timeout"}
            except Exception as e:
                return 500, {"message": str(e)}
    
    async def get_file_sha(self, owner: str, repo_name: str, filename: str, branch: str) -> Optional[str]:
        """Get SHA hash of existing file"""
        status, response = await self.api_request(
            "GET",
            f"/repos/{owner}/{repo_name}/contents/{filename}?ref={branch}"
        )
        
        if status == 200:
            return response.get('sha')
        return None
    
    async def create_file(self, user_id: int, filename: str, content: str) -> Tuple[bool, str]:
        """Create a new file in user's current repo/branch"""
        # ALWAYS get fresh context from database
        repo, branch, commit_msg = await self.get_user_context(user_id)
        
        if not repo or repo == DEFAULT_REPO:
            return False, "Please select a repository first using `--repo <name>`"
        
        owner, repo_name = self.parse_repo_full_name(repo)
        
        # Encode content
        encoded_content = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        
        # Create file
        status, response = await self.api_request(
            "PUT",
            f"/repos/{owner}/{repo_name}/contents/{filename}",
            {
                "message": commit_msg,
                "content": encoded_content,
                "branch": branch
            }
        )
        
        if status == 201:
            return True, f"Created `{filename}` in `{repo}` (branch: {branch})"
        else:
            error = response.get('message', f'Unknown error (status: {status})')
            return False, f"Error: {error}"
    
    async def edit_file(self, user_id: int, filename: str, content: str) -> Tuple[bool, str]:
        """Edit an existing file"""
        # ALWAYS get fresh context from database
        repo, branch, commit_msg = await self.get_user_context(user_id)
        
        if not repo or repo == DEFAULT_REPO:
            return False, "Please select a repository first using `--repo <name>`"
        
        owner, repo_name = self.parse_repo_full_name(repo)
        
        # Get existing file SHA
        sha = await self.get_file_sha(owner, repo_name, filename, branch)
        if not sha:
            return False, f"File `{filename}` not found in repository `{repo}`"
        
        # Encode content
        encoded_content = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        
        # Update file
        status, response = await self.api_request(
            "PUT",
            f"/repos/{owner}/{repo_name}/contents/{filename}",
            {
                "message": commit_msg,
                "content": encoded_content,
                "sha": sha,
                "branch": branch
            }
        )
        
        if status == 200:
            return True, f"Updated `{filename}` in `{repo}` (branch: {branch})"
        else:
            error = response.get('message', f'Unknown error (status: {status})')
            return False, f"Error: {error}"
    
    async def delete_file(self, user_id: int, filename: str) -> Tuple[bool, str]:
        """Delete a file"""
        # ALWAYS get fresh context from database
        repo, branch, commit_msg = await self.get_user_context(user_id)
        
        if not repo or repo == DEFAULT_REPO:
            return False, "Please select a repository first using `--repo <name>`"
        
        owner, repo_name = self.parse_repo_full_name(repo)
        
        # Get existing file SHA
        sha = await self.get_file_sha(owner, repo_name, filename, branch)
        if not sha:
            return False, f"File `{filename}` not found in repository `{repo}`"
        
        # Delete file
        status, response = await self.api_request(
            "DELETE",
            f"/repos/{owner}/{repo_name}/contents/{filename}",
            {
                "message": commit_msg,
                "sha": sha,
                "branch": branch
            }
        )
        
        if status == 200:
            return True, f"Deleted `{filename}` from `{repo}` (branch: {branch})"
        else:
            error = response.get('message', f'Unknown error (status: {status})')
            return False, f"Error: {error}"
    
    async def view_file(self, user_id: int, filename: str) -> Tuple[bool, str, Optional[str]]:
        """View file content"""
        # ALWAYS get fresh context from database
        repo, branch, _ = await self.get_user_context(user_id)
        
        if not repo or repo == DEFAULT_REPO:
            return False, "Please select a repository first using `--repo <name>`", None
        
        owner, repo_name = self.parse_repo_full_name(repo)
        
        # Get file content
        status, response = await self.api_request(
            "GET",
            f"/repos/{owner}/{repo_name}/contents/{filename}?ref={branch}"
        )
        
        if status == 200:
            try:
                content = base64.b64decode(response['content']).decode('utf-8')
                return True, f"File `{filename}` from `{repo}` (branch: {branch})", content
            except:
                return False, "Failed to decode file content", None
        else:
            error = response.get('message', 'File not found')
            return False, f"Error: {error}", None
    
    async def list_files(self, user_id: int, path: str = "") -> Tuple[bool, str, List[Dict]]:
        """List files in repository"""
        # ALWAYS get fresh context from database
        repo, branch, _ = await self.get_user_context(user_id)
        
        if not repo or repo == DEFAULT_REPO:
            return False, "Please select a repository first using `--repo <name>`", []
        
        owner, repo_name = self.parse_repo_full_name(repo)
        
        # Get repository contents
        endpoint = f"/repos/{owner}/{repo_name}/contents/{path}?ref={branch}" if path else f"/repos/{owner}/{repo_name}/contents?ref={branch}"
        status, response = await self.api_request("GET", endpoint)
        
        if status == 200:
            return True, f"Contents of `{repo}` (branch: {branch})", response
        else:
            error = response.get('message', f'Repository not found (status: {status})')
            return False, f"Error: {error}", []
    
    async def get_branches(self, user_id: int) -> Tuple[bool, str, List[str]]:
        """Get list of branches in current repository"""
        # ALWAYS get fresh context from database
        repo, _, _ = await self.get_user_context(user_id)
        
        if not repo or repo == DEFAULT_REPO:
            return False, "Please select a repository first using `--repo <name>`", []
        
        owner, repo_name = self.parse_repo_full_name(repo)
        
        # Get branches
        status, response = await self.api_request(
            "GET",
            f"/repos/{owner}/{repo_name}/branches"
        )
        
        if status == 200:
            branches = [branch['name'] for branch in response]
            return True, f"Branches in `{repo}`", branches
        else:
            error = response.get('message', f'Unknown error (status: {status})')
            return False, f"Error: {error}", []
    
    async def create_branch(self, user_id: int, branch_name: str) -> Tuple[bool, str]:
        """Create a new branch"""
        # ALWAYS get fresh context from database
        repo, _, _ = await self.get_user_context(user_id)
        
        if not repo or repo == DEFAULT_REPO:
            return False, "Please select a repository first using `--repo <name>`"
        
        owner, repo_name = self.parse_repo_full_name(repo)
        
        # Get default branch SHA
        default_branch_status, default_branch_response = await self.api_request(
            "GET",
            f"/repos/{owner}/{repo_name}/git/refs/heads/main"
        )
        
        if default_branch_status != 200:
            # Try master instead
            default_branch_status, default_branch_response = await self.api_request(
                "GET",
                f"/repos/{owner}/{repo_name}/git/refs/heads/master"
            )
        
        if default_branch_status == 200:
            sha = default_branch_response['object']['sha']
            
            # Create new branch
            create_status, create_response = await self.api_request(
                "POST",
                f"/repos/{owner}/{repo_name}/git/refs",
                {
                    "ref": f"refs/heads/{branch_name}",
                    "sha": sha
                }
            )
            
            if create_status == 201:
                await self.db.set_user_branch(user_id, branch_name)
                return True, f"Created and switched to branch: `{branch_name}`"
            else:
                error = create_response.get('message', 'Failed to create branch')
                return False, f"Error: {error}"
        else:
            return False, "Could not find default branch to create from"

# ========== HELPER FUNCTIONS ==========

def sanitize_filename(filename: str) -> str:
    """Prevent path traversal and sanitize filenames"""
    filename = filename.replace("..", "").replace("/", "").replace("\\", "")
    filename = re.sub(r'[^a-zA-Z0-9._-]', '', filename)
    return filename

# ========== GITHUB COMMANDS COG ==========

class GitHubCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    async def _ensure_github_ready(self, ctx) -> bool:
        """Ensure GitHub client is ready"""
        if not self.bot.github_client or not self.bot.github_handler:
            await ctx.send("GitHub client not ready. Please wait for bot initialization.")
            return False
        return True
    
    @commands.command(name='repo')
    async def cmd_repo(self, ctx, repo_name: str = None, private: str = "true"):
        """Switch to or create a repository"""
        if not await self._ensure_github_ready(ctx):
            return
        
        if not repo_name:
            # ALWAYS get fresh from database
            current = await self.bot.db.get_user_repo(ctx.author.id)
            await ctx.send(f"Current repository: `{current}`")
            return
        
        repo_name = sanitize_filename(repo_name)
        is_private = private.lower() in ['true', 'yes', '1', 'private']
        
        try:
            # Try to access the repository
            repo_full_name = f"{self.bot.github_username}/{repo_name}"
            repo = self.bot.github_client.get_repo(repo_full_name)
            
            # Success - repository exists
            await self.bot.db.set_user_repo(ctx.author.id, repo_full_name)
            
            embed = discord.Embed(
                title="Repository Switched",
                description=f"Now working in **{repo.name}**",
                color=discord.Color.green()
            )
            embed.add_field(name="URL", value=repo.html_url, inline=False)
            embed.add_field(name="Visibility", value="Private" if repo.private else "Public", inline=True)
            embed.add_field(name="Owner", value=repo.owner.login, inline=True)
            
            await ctx.send(embed=embed)
            
        except GithubException as e:
            # Repository doesn't exist - offer to create
            error_msg = str(e)
            if "404" in error_msg or "Not Found" in error_msg:
                embed = discord.Embed(
                    title="Repository Not Found",
                    description=f"Repository `{repo_name}` doesn't exist under `{self.bot.github_username}`.",
                    color=discord.Color.orange()
                )
                embed.add_field(
                    name="Create it?",
                    value="Type `create` to create this repository or `cancel` to abort.",
                    inline=False
                )
                
                await ctx.send(embed=embed)
                
                # Wait for user response
                def check(m):
                    return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() in ['create', 'cancel']
                
                try:
                    msg = await self.bot.wait_for('message', timeout=30.0, check=check)
                    
                    if msg.content.lower() == 'create':
                        # Create the repository
                        user = self.bot.github_client.get_user()
                        repo = user.create_repo(
                            name=repo_name,
                            private=is_private,
                            auto_init=False,
                            description="Created via GitCord Bot"
                        )
                        
                        repo_full_name = f"{self.bot.github_username}/{repo_name}"
                        await self.bot.db.set_user_repo(ctx.author.id, repo_full_name)
                        
                        embed = discord.Embed(
                            title="Repository Created",
                            description=f"Created and switched to **{repo.name}**",
                            color=discord.Color.green()
                        )
                        embed.add_field(name="URL", value=repo.html_url, inline=False)
                        embed.add_field(name="Visibility", value="Private" if is_private else "Public", inline=True)
                        
                        await ctx.send(embed=embed)
                    else:
                        await ctx.send("Repository creation cancelled.")
                        
                except asyncio.TimeoutError:
                    await ctx.send("Repository creation timed out.")
            else:
                await ctx.send(f"GitHub Error: {error_msg}")
                
        except Exception as e:
            await ctx.send(f"Error: {str(e)}")
    
    @commands.command(name='create')
    async def cmd_create(self, ctx, filename: str, *, content: str):
        """Create a new file"""
        if not await self._ensure_github_ready(ctx):
            return
        
        filename = sanitize_filename(filename)
        
        if len(content) > 10000:
            await ctx.send("File too large (max 10KB)")
            return
        
        # Use GitHub handler which ALWAYS gets fresh context
        success, message = await self.bot.github_handler.create_file(ctx.author.id, filename, content)
        await ctx.send(message)
    
    @commands.command(name='edit')
    async def cmd_edit(self, ctx, filename: str, *, content: str):
        """Edit an existing file"""
        if not await self._ensure_github_ready(ctx):
            return
        
        filename = sanitize_filename(filename)
        
        if len(content) > 10000:
            await ctx.send("File too large (max 10KB)")
            return
        
        # Use GitHub handler which ALWAYS gets fresh context
        success, message = await self.bot.github_handler.edit_file(ctx.author.id, filename, content)
        await ctx.send(message)
    
    @commands.command(name='view')
    async def cmd_view(self, ctx, filename: str):
        """View a file"""
        if not await self._ensure_github_ready(ctx):
            return
        
        filename = sanitize_filename(filename)
        
        # Use GitHub handler which ALWAYS gets fresh context
        success, message, content = await self.bot.github_handler.view_file(ctx.author.id, filename)
        
        if success and content:
            # Truncate if too long
            if len(content) > 1500:
                content = content[:1500] + "\n... (truncated)"
            
            # Syntax highlighting
            ext = filename.split('.')[-1].lower() if '.' in filename else 'txt'
            languages = {
                'py': 'python', 'js': 'javascript', 'ts': 'typescript',
                'html': 'html', 'css': 'css', 'json': 'json',
                'md': 'markdown', 'txt': 'text'
            }
            lang = languages.get(ext, 'text')
            
            embed = discord.Embed(
                title=f"{filename}",
                description=message,
                color=discord.Color.blue()
            )
            embed.add_field(name="Content", value=f"```{lang}\n{content}\n```", inline=False)
            
            await ctx.send(embed=embed)
        else:
            await ctx.send(message)
    
    @commands.command(name='list')
    async def cmd_list(self, ctx, path: str = ""):
        """List files in current repository"""
        if not await self._ensure_github_ready(ctx):
            return
        
        # Use GitHub handler which ALWAYS gets fresh context
        success, message, files = await self.bot.github_handler.list_files(ctx.author.id, path)
        
        if success:
            files_list = []
            directories = []
            
            for item in files:
                item_type = item.get('type', 'unknown')
                name = item.get('name', 'unknown')
                if item_type == 'file':
                    size = item.get('size', 0)
                    files_list.append(f"{name} ({size} bytes)")
                elif item_type == 'dir':
                    directories.append(f"{name}/")
            
            embed = discord.Embed(
                title=message,
                color=discord.Color.purple()
            )
            
            if directories:
                embed.add_field(name="Directories", value="\n".join(directories[:20]), inline=False)
            
            if files_list:
                embed.add_field(name="Files", value="\n".join(files_list[:20]), inline=False)
            
            if not files_list and not directories:
                embed.description = "This directory is empty"
            elif len(files_list) > 20 or len(directories) > 20:
                embed.set_footer(text="Showing first 20 items each")
            
            await ctx.send(embed=embed)
        else:
            await ctx.send(message)
    
    @commands.command(name='current')
    async def cmd_current(self, ctx):
        """Show current repository settings"""
        # ALWAYS get fresh from database
        repo = await self.bot.db.get_user_repo(ctx.author.id)
        branch = await self.bot.db.get_user_branch(ctx.author.id)
        commit_msg = await self.bot.db.get_user_commit_message(ctx.author.id)
        
        embed = discord.Embed(
            title="Current Settings",
            color=discord.Color.blue()
        )
        embed.add_field(name="Repository", value=f"`{repo}`", inline=False)
        embed.add_field(name="Branch", value=f"`{branch}`", inline=True)
        embed.add_field(name="Commit Message", value=f"`{commit_msg}`", inline=True)
        
        await ctx.send(embed=embed)
    
    @commands.command(name='delete')
    async def cmd_delete(self, ctx, filename: str):
        """Delete a file"""
        if not await self._ensure_github_ready(ctx):
            return
        
        filename = sanitize_filename(filename)
        
        # Use GitHub handler which ALWAYS gets fresh context
        success, message = await self.bot.github_handler.delete_file(ctx.author.id, filename)
        await ctx.send(message)
    
    @commands.command(name='branch')
    async def cmd_branch(self, ctx, branch_name: str = None):
        """Switch branch or list branches"""
        if not await self._ensure_github_ready(ctx):
            return
        
        if not branch_name:
            # Use GitHub handler which ALWAYS gets fresh context
            success, message, branches = await self.bot.github_handler.get_branches(ctx.author.id)
            
            if success:
                # ALWAYS get fresh from database for current branch
                current_branch = await self.bot.db.get_user_branch(ctx.author.id)
                
                embed = discord.Embed(
                    title=message,
                    color=discord.Color.purple()
                )
                
                branch_list = []
                for branch in branches[:20]:
                    if branch == current_branch:
                        branch_list.append(f"**{branch}** (current)")
                    else:
                        branch_list.append(branch)
                
                embed.description = "\n".join(branch_list)
                
                if len(branches) > 20:
                    embed.set_footer(text=f"Showing first 20 of {len(branches)} branches")
                
                await ctx.send(embed=embed)
            else:
                await ctx.send(message)
        else:
            # ALWAYS get fresh context first
            repo, _, _ = await self.bot.github_handler.get_user_context(ctx.author.id)
            if not repo or repo == DEFAULT_REPO:
                await ctx.send("Please select a repository first using `--repo <name>`")
                return
            
            success, message, branches = await self.bot.github_handler.get_branches(ctx.author.id)
            
            if success and branch_name in branches:
                await self.bot.db.set_user_branch(ctx.author.id, branch_name)
                await ctx.send(f"Switched to branch: `{branch_name}`")
            else:
                # Branch doesn't exist, ask to create
                embed = discord.Embed(
                    title="Branch Not Found",
                    description=f"Branch `{branch_name}` doesn't exist.",
                    color=discord.Color.orange()
                )
                embed.add_field(
                    name="Create it?",
                    value="Type `create` to create this branch or `cancel` to abort.",
                    inline=False
                )
                
                await ctx.send(embed=embed)
                
                def check(m):
                    return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() in ['create', 'cancel']
                
                try:
                    msg = await self.bot.wait_for('message', timeout=30.0, check=check)
                    
                    if msg.content.lower() == 'create':
                        success, message = await self.bot.github_handler.create_branch(ctx.author.id, branch_name)
                        await ctx.send(message)
                    else:
                        await ctx.send("Branch creation cancelled.")
                        
                except asyncio.TimeoutError:
                    await ctx.send("Branch creation timed out.")
    
    @commands.command(name='commit')
    async def cmd_commit(self, ctx, *, message: str = None):
        """Set commit message"""
        if not message:
            # ALWAYS get fresh from database
            current_message = await self.bot.db.get_user_commit_message(ctx.author.id)
            await ctx.send(f"Current commit message: `{current_message}`")
            return
        
        await self.bot.db.set_user_commit_message(ctx.author.id, message)
        await ctx.send(f"Commit message set to: `{message}`")
    
    @commands.command(name='prefix')
    async def cmd_prefix(self, ctx, new_prefix: str = None):
        """Change command prefix"""
        if not new_prefix:
            # ALWAYS get fresh from database
            current_prefix = await self.bot.db.get_user_prefix(ctx.author.id)
            await ctx.send(f"Current prefix: `{current_prefix}`")
            return
        
        # Validate prefix
        if len(new_prefix) > 5:
            await ctx.send("Prefix must be 5 characters or less")
            return
        
        if any(char.isspace() for char in new_prefix):
            await ctx.send("Prefix cannot contain spaces")
            return
        
        await self.bot.db.set_user_prefix(ctx.author.id, new_prefix)
        await ctx.send(f"Command prefix changed to: `{new_prefix}`")
    
    @commands.command(name='help')
    async def cmd_help(self, ctx):
        """Show all commands"""
        # ALWAYS get fresh from database
        current_prefix = await self.bot.db.get_user_prefix(ctx.author.id)
        
        embed = discord.Embed(
            title="GitCord Commands",
            description="A Discord bot for GitHub repository management",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="Repository Commands",
            value=f"`{current_prefix}repo [name] [private]` - Switch to/create repository\n"
                  f"`{current_prefix}current` - Show current repository/branch\n"
                  f"`{current_prefix}list [path]` - List repository contents",
            inline=False
        )
        
        embed.add_field(
            name="File Commands",
            value=f"`{current_prefix}create [filename] [content]` - Create file\n"
                  f"`{current_prefix}edit [filename] [content]` - Edit file\n"
                  f"`{current_prefix}view [filename]` - View file\n"
                  f"`{current_prefix}delete [filename]` - Delete file",
            inline=False
        )
        
        embed.add_field(
            name="Branch & Commit Commands",
            value=f"`{current_prefix}branch [name]` - Switch branch (blank to list)\n"
                  f"`{current_prefix}commit [message]` - Set commit message",
            inline=False
        )
        
        embed.add_field(
            name="Utility Commands",
            value=f"`{current_prefix}prefix [new_prefix]` - Change command prefix\n"
                  f"`{current_prefix}help` - Show this help",
            inline=False
        )
        
        embed.add_field(
            name="Admin Commands",
            value=f"`{current_prefix}restart` - Restart bot (owner only)",
            inline=False
        )
        
        embed.add_field(
            name="Slash Commands",
            value="All commands are also available as slash commands (`/repo`, `/create`, etc.)",
            inline=False
        )
        
        embed.set_footer(text=f"Default prefix: {DEFAULT_PREFIX}")
        
        await ctx.send(embed=embed)

    # ========== SLASH COMMANDS ==========
    
    @app_commands.command(name="repo", description="Switch to or create a repository")
    @app_commands.describe(
        repo_name="Repository name",
        private="Make repository private"
    )
    async def slash_repo(self, interaction: discord.Interaction, repo_name: str, private: bool = True):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_repo(ctx, repo_name, "true" if private else "false")
    
    @app_commands.command(name="create", description="Create a new file")
    @app_commands.describe(
        filename="File name",
        content="File content"
    )
    async def slash_create(self, interaction: discord.Interaction, filename: str, content: str):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_create(ctx, filename, content=content)
    
    @app_commands.command(name="edit", description="Edit an existing file")
    @app_commands.describe(
        filename="File name",
        content="New file content"
    )
    async def slash_edit(self, interaction: discord.Interaction, filename: str, content: str):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_edit(ctx, filename, content=content)
    
    @app_commands.command(name="view", description="View a file")
    @app_commands.describe(
        filename="File name"
    )
    async def slash_view(self, interaction: discord.Interaction, filename: str):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_view(ctx, filename)
    
    @app_commands.command(name="list", description="List repository files")
    @app_commands.describe(
        path="Optional path within repository"
    )
    async def slash_list(self, interaction: discord.Interaction, path: str = ""):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_list(ctx, path)
    
    @app_commands.command(name="current", description="Show current repository settings")
    async def slash_current(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_current(ctx)
    
    @app_commands.command(name="delete", description="Delete a file")
    @app_commands.describe(
        filename="File name"
    )
    async def slash_delete(self, interaction: discord.Interaction, filename: str):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_delete(ctx, filename)
    
    @app_commands.command(name="branch", description="Switch branch or list branches")
    @app_commands.describe(
        branch_name="Branch name (leave blank to list)"
    )
    async def slash_branch(self, interaction: discord.Interaction, branch_name: str = ""):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_branch(ctx, branch_name if branch_name else None)
    
    @app_commands.command(name="commit", description="Set commit message")
    @app_commands.describe(
        message="Commit message"
    )
    async def slash_commit(self, interaction: discord.Interaction, message: str = ""):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_commit(ctx, message if message else None)
    
    @app_commands.command(name="prefix", description="Change command prefix")
    @app_commands.describe(
        new_prefix="New prefix"
    )
    async def slash_prefix(self, interaction: discord.Interaction, new_prefix: str = ""):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_prefix(ctx, new_prefix if new_prefix else None)
    
    @app_commands.command(name="help", description="Show all commands")
    async def slash_help(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ctx = await self.bot.get_context(interaction)
        await self.cmd_help(ctx)

# ========== UTILITY COMMANDS COG ==========

class UtilityCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command(name='ping')
    async def cmd_ping(self, ctx):
        """Check bot latency"""
        latency = round(self.bot.latency * 1000)
        await ctx.send(f"Pong! Latency: {latency}ms")

# ========== ADMIN COMMANDS COG ==========

class AdminCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command(name='restart')
    @commands.is_owner()
    async def cmd_restart(self, ctx):
        """Restart the bot (owner only)"""
        await ctx.send("Restarting bot...")
        print("Bot restart initiated")
        os.execv(sys.executable, ['python'] + sys.argv)

# ========== ERROR HANDLING ==========

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        # ALWAYS get fresh from database
        current_prefix = await bot.db.get_user_prefix(ctx.author.id) if bot.db else DEFAULT_PREFIX
        await ctx.send(f"Command not found. Use `{current_prefix}help` for available commands.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing required argument. Use `{ctx.command.name} [arguments]`")
    elif isinstance(error, commands.NotOwner):
        await ctx.send("This command is for bot owners only.")
    else:
        print(f"Command Error: {error}")
        await ctx.send(f"An error occurred: {str(error)}")

# ========== START BOT ==========

if __name__ == "__main__":
    print("=" * 50)
    print("Starting GitCord Bot...")
    print("=" * 50)
    
    try:
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        print("\nBot stopped by user")
    except discord.LoginFailure:
        print("Failed to login to Discord. Check your DISCORD_TOKEN.")
    except Exception as e:
        print(f"Bot crashed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("Bot shutdown complete")
