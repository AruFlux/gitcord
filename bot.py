import discord
from discord import app_commands
import aiohttp
import os
from typing import Optional
from github import Github, GithubException
from dotenv import load_dotenv
import re

# Load environment variables
load_dotenv()

# Configuration from Railway environment variables
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME")
DEFAULT_REPO = os.getenv("DEFAULT_REPO", "my-discord-projects")

# Initialize bot
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# GitHub client
github_client = Github(GITHUB_TOKEN)

# Store user's current repo (in-memory, consider database for production)
user_repos = {}

def sanitize_filename(filename: str) -> str:
    """Sanitize filename to be GitHub-safe"""
    # Remove any path traversal attempts
    filename = filename.replace("..", "").replace("/", "").replace("\\", "")
    # Remove non-alphanumeric characters except dots, hyphens, underscores
    filename = re.sub(r'[^a-zA-Z0-9._-]', '', filename)
    return filename

def get_user_repo(user_id: int) -> str:
    """Get current repository for a user"""
    return user_repos.get(user_id, DEFAULT_REPO)

def set_user_repo(user_id: int, repo_name: str):
    """Set current repository for a user"""
    user_repos[user_id] = repo_name

async def create_github_repo(repo_name: str, is_private: bool = True):
    """Create a new GitHub repository"""
    try:
        user = github_client.get_user()
        repo = user.create_repo(
            name=repo_name,
            private=is_private,
            auto_init=True,
            description="Created via GitCord Discord Bot"
        )
        return repo, None
    except GithubException as e:
        return None, str(e)

async def file_exists(repo_name: str, filename: str) -> bool:
    """Check if a file exists in the repository"""
    try:
        repo = github_client.get_repo(f"{GITHUB_USERNAME}/{repo_name}")
        try:
            repo.get_contents(filename)
            return True
        except:
            return False
    except:
        return False

@bot.event
async def on_ready():
    print(f'✅ GitCord Bot is online as {bot.user}!')
    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"❌ Error syncing commands: {e}")

# ========== SLASH COMMANDS ==========

@tree.command(
    name="repo",
    description="Switch to a different GitHub repository"
)
@app_commands.describe(
    repo_name="Repository name (will be created if it doesn't exist)",
    private="Make repository private (default: true)"
)
async def switch_repo(
    interaction: discord.Interaction,
    repo_name: str,
    private: bool = True
):
    """Switch to a GitHub repository, create if it doesn't exist"""
    await interaction.response.defer(thinking=True)
    
    repo_name = sanitize_filename(repo_name)
    
    try:
        # Try to access the repo
        repo = github_client.get_repo(f"{GITHUB_USERNAME}/{repo_name}")
        set_user_repo(interaction.user.id, repo_name)
        
        embed = discord.Embed(
            title="✅ Repository Switched",
            description=f"Now working in: **{repo_name}**",
            color=discord.Color.green()
        )
        embed.add_field(name="URL", value=repo.html_url, inline=False)
        embed.add_field(name="Visibility", value="🔒 Private" if repo.private else "🌐 Public", inline=True)
        
        await interaction.followup.send(embed=embed)
        
    except GithubException:
        # Repository doesn't exist, ask to create it
        view = discord.ui.View()
        
        async def create_repo_callback(interaction: discord.Interaction):
            repo, error = await create_github_repo(repo_name, private)
            if repo:
                set_user_repo(interaction.user.id, repo_name)
                embed = discord.Embed(
                    title="✨ Repository Created & Switched",
                    description=f"Created and switched to: **{repo_name}**",
                    color=discord.Color.blue()
                )
                embed.add_field(name="URL", value=repo.html_url, inline=False)
                embed.add_field(name="Visibility", value="🔒 Private" if private else "🌐 Public", inline=True)
                await interaction.response.edit_message(embed=embed, view=None)
            else:
                await interaction.response.edit_message(
                    content=f"❌ Failed to create repository: {error}",
                    view=None
                )
        
        async def cancel_callback(interaction: discord.Interaction):
            await interaction.response.edit_message(
                content="❌ Repository creation cancelled.",
                view=None
            )
        
        create_button = discord.ui.Button(label="Create Repository", style=discord.ButtonStyle.green)
        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.red)
        
        create_button.callback = create_repo_callback
        cancel_button.callback = cancel_callback
        
        view.add_item(create_button)
        view.add_item(cancel_button)
        
        embed = discord.Embed(
            title="Repository Not Found",
            description=f"Repository `{repo_name}` doesn't exist.",
            color=discord.Color.orange()
        )
        embed.add_field(name="Would you like to create it?", value="", inline=False)
        
        await interaction.followup.send(embed=embed, view=view)

@tree.command(
    name="create",
    description="Create a new file in current repository"
)
@app_commands.describe(
    filename="Name of the file to create (supports .py, .js, .txt, .md, etc.)",
    content="Content of the file"
)
async def create_file(
    interaction: discord.Interaction,
    filename: str,
    content: str
):
    """Create a new file in GitHub"""
    await interaction.response.defer(thinking=True)
    
    repo_name = get_user_repo(interaction.user.id)
    filename = sanitize_filename(filename)
    
    # Check if file already exists
    if await file_exists(repo_name, filename):
        await interaction.followup.send(
            f"❌ File `{filename}` already exists in `{repo_name}`! Use `/edit` to modify it."
        )
        return
    
    api_url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}"
    
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    data = {
        "message": f"Create {filename} via GitCord",
        "content": content.encode("utf-8").hex(),
        "branch": "main"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.put(api_url, headers=headers, json=data) as response:
            if response.status == 201:
                repo_url = f"https://github.com/{GITHUB_USERNAME}/{repo_name}/blob/main/{filename}"
                
                embed = discord.Embed(
                    title="📄 File Created",
                    description=f"File `{filename}` created in `{repo_name}`",
                    color=discord.Color.green()
                )
                embed.add_field(name="Repository", value=repo_name, inline=True)
                embed.add_field(name="Preview", value=f"```{content[:100]}...```" if len(content) > 100 else f"```{content}```", inline=False)
                embed.add_field(name="URL", value=f"[View on GitHub]({repo_url})", inline=False)
                
                await interaction.followup.send(embed=embed)
            else:
                error_data = await response.json()
                await interaction.followup.send(f"❌ Error: {error_data.get('message', 'Unknown error')}")

@tree.command(
    name="edit",
    description="Edit an existing file"
)
@app_commands.describe(
    filename="Name of the file to edit",
    content="New content"
)
async def edit_file(
    interaction: discord.Interaction,
    filename: str,
    content: str
):
    """Edit an existing file"""
    await interaction.response.defer(thinking=True)
    
    repo_name = get_user_repo(interaction.user.id)
    filename = sanitize_filename(filename)
    
    # First get the file to get its SHA
    api_url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}"
    
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    async with aiohttp.ClientSession() as session:
        # Get file info
        async with session.get(api_url, headers=headers) as response:
            if response.status != 200:
                await interaction.followup.send(f"❌ File `{filename}` not found in `{repo_name}`!")
                return
            
            file_data = await response.json()
            sha = file_data["sha"]
        
        # Update the file
        data = {
            "message": f"Update {filename} via GitCord",
            "content": content.encode("utf-8").hex(),
            "sha": sha,
            "branch": "main"
        }
        
        async with session.put(api_url, headers=headers, json=data) as response:
            if response.status == 200:
                repo_url = f"https://github.com/{GITHUB_USERNAME}/{repo_name}/blob/main/{filename}"
                
                embed = discord.Embed(
                    title="✏️ File Updated",
                    description=f"File `{filename}` updated in `{repo_name}`",
                    color=discord.Color.blue()
                )
                embed.add_field(name="Changes", value=f"```{content[:100]}...```" if len(content) > 100 else f"```{content}```", inline=False)
                embed.add_field(name="URL", value=f"[View on GitHub]({repo_url})", inline=False)
                
                await interaction.followup.send(embed=embed)
            else:
                error_data = await response.json()
                await interaction.followup.send(f"❌ Error: {error_data.get('message', 'Unknown error')}")

@tree.command(
    name="view",
    description="View a file's content"
)
@app_commands.describe(
    filename="Name of the file to view"
)
async def view_file(
    interaction: discord.Interaction,
    filename: str
):
    """View a file's content"""
    await interaction.response.defer(thinking=True)
    
    repo_name = get_user_repo(interaction.user.id)
    filename = sanitize_filename(filename)
    
    api_url = f"https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/{filename}"
    
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(api_url, headers=headers) as response:
            if response.status != 200:
                await interaction.followup.send(f"❌ File `{filename}` not found in `{repo_name}`!")
                return
            
            file_data = await response.json()
            import base64
            content = base64.b64decode(file_data["content"]).decode("utf-8")
            file_url = file_data["html_url"]
            
            # Truncate if too long for Discord
            if len(content) > 1000:
                preview = content[:1000] + "..."
            else:
                preview = content
            
            embed = discord.Embed(
                title=f"📖 {filename}",
                description=f"Content from `{repo_name}`",
                color=discord.Color.purple()
            )
            embed.add_field(name="Content", value=f"```{preview}```", inline=False)
            embed.add_field(name="URL", value=f"[View on GitHub]({file_url})", inline=False)
            
            await interaction.followup.send(embed=embed)

@tree.command(
    name="list",
    description="List files in current repository"
)
async def list_files(interaction: discord.Interaction):
    """List files in the current repository"""
    await interaction.response.defer(thinking=True)
    
    repo_name = get_user_repo(interaction.user.id)
    
    try:
        repo = github_client.get_repo(f"{GITHUB_USERNAME}/{repo_name}")
        contents = repo.get_contents("")
        
        files = []
        for content in contents:
            files.append(f"• `{content.name}` ({'📁' if content.type == 'dir' else '📄'})")
        
        embed = discord.Embed(
            title=f"📁 Files in {repo_name}",
            description="\n".join(files[:20]),  # Limit to 20 files
            color=discord.Color.gold()
        )
        embed.set_footer(text=f"Total: {len(files)} files")
        
        await interaction.followup.send(embed=embed)
    except GithubException as e:
        await interaction.followup.send(f"❌ Error: {str(e)}")

@tree.command(
    name="current",
    description="Show current repository"
)
async def current_repo(interaction: discord.Interaction):
    """Show which repository you're currently working in"""
    repo_name = get_user_repo(interaction.user.id)
    
    try:
        repo = github_client.get_repo(f"{GITHUB_USERNAME}/{repo_name}")
        
        embed = discord.Embed(
            title="Current Repository",
            color=discord.Color.blurple()
        )
        embed.add_field(name="Name", value=repo_name, inline=True)
        embed.add_field(name="Visibility", value="🔒 Private" if repo.private else "🌐 Public", inline=True)
        embed.add_field(name="URL", value=repo.html_url, inline=False)
        
        await interaction.response.send_message(embed=embed)
    except:
        await interaction.response.send_message(
            f"📌 You're working in: **{repo_name}**\n"
            f"(This repository will be created when you first use it)"
        )

# ========== ERROR HANDLER ==========

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⏳ Please wait {error.retry_after:.1f} seconds before using this command again.",
            ephemeral=True
        )
    else:
        print(f"Error: {error}")
        await interaction.response.send_message(
            "❌ An error occurred while executing the command.",
            ephemeral=True
        )

# Run the bot
if __name__ == "__main__":
    if not DISCORD_TOKEN or not GITHUB_TOKEN:
        print("❌ Missing environment variables. Please set DISCORD_TOKEN and GITHUB_TOKEN.")
    else:
        bot.run(DISCORD_TOKEN)
