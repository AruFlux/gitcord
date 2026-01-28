# GitCord

A Discord bot that provides seamless integration between Discord and GitHub, allowing you to manage repositories, files, and code directly from your Discord server.

## Features
- Create new GitHub repositories on-demand
- Create, edit, view, and delete files
- Automatic repository creation
- Repository switching per user
- Branch management
- Dual command system (prefix + slash commands)
- Secure file operations with input sanitization

## Command Systems

### Prefix Commands (Default: `--`)
```
--repo [name] [private=true/false]    Switch to or create a repository
--create [filename] [content]          Create a new file
--edit [filename] [content]            Edit an existing file
--view [filename]                      View file content
--list                                 List repository files
--current                              Show current repository
--delete [filename]                    Delete a file
--branch [name]                        Switch branch (blank to list)
--commit [message]                     Set commit message
--prefix [new_prefix]                  Change command prefix
--restart                              Restart bot (owner only)
--help                                 Show all commands
```

### Slash Commands (/)
All prefix commands are also available as slash commands:
- `/repo` - `/create` - `/edit` - `/view` - `/list`
- `/current` - `/delete` - `/branch` - `/prefix` - `/help`

## Usage Examples

### Basic Workflow
```
--repo my-project
--create hello.py print("Hello from Discord!")
--edit hello.py print("Updated via Discord Bot")
--view hello.py
--list
```

### Advanced Usage
```
--branch development
--create utils.py def helper(): return "Helper function"
--commit "Add utility functions"
--prefix !
!help
```

## Railway Deployment

### Step 1: Push to GitHub
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/gitcord-bot.git
git push -u origin main
```

### Step 2: Connect to Railway
1. Go to [Railway.app](https://railway.app)
2. Click "New Project" → "Deploy from GitHub repo"
3. Select your repository
4. Railway will auto-detect Python configuration

### Step 3: Configure Environment Variables
In Railway dashboard, add these variables:
```
DISCORD_TOKEN=your_discord_bot_token_here
GITHUB_TOKEN=your_github_personal_access_token
GITHUB_USERNAME=your_github_username
DEFAULT_REPO=discord-projects
```

### Step 4: Deploy
Railway will automatically deploy. Check the "Deployments" tab for logs.

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `DISCORD_TOKEN` | Your Discord bot token from Discord Developer Portal | Yes |
| `GITHUB_TOKEN` | GitHub Personal Access Token with `repo` scope | Yes |
| `GITHUB_USERNAME` | Your GitHub username | Yes |
| `DEFAULT_REPO` | Default repository name | No |

## Getting Tokens

### Discord Bot Token
1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a new application
3. Go to "Bot" section → "Reset Token"
4. Enable "SERVER MEMBERS INTENT" and "MESSAGE CONTENT INTENT"

### GitHub Personal Access Token
1. Go to GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
2. Generate new token with `repo` scope
3. Save token immediately (won't be shown again)

## Security Notes

1. Never commit tokens to GitHub
2. Use Railway environment variables for secrets
3. The bot sanitizes all file names and paths
4. Consider restricting bot access to trusted users
5. Monitor your GitHub account for unexpected activity

## Troubleshooting

### Bot Not Responding
1. Check Railway logs for errors
2. Verify environment variables are set correctly
3. Ensure bot has proper Discord permissions

### GitHub Authentication Errors
1. Verify GitHub token has `repo` scope
2. Check token hasn't expired
3. Ensure GITHUB_USERNAME is correct

### File Operations Fail
1. Check repository exists
2. Verify you have write permissions
3. Ensure file names are valid (no special characters)

## Support

For issues or feature requests, check Railway deployment logs and verify token permissions.

---

**Project Structure:**
```
gitcord-bot/
├── bot.py              # Main bot file
├── requirements.txt    # Python dependencies
├── .env.example       # Environment variables template
├── railway.toml       # Railway deployment config
└── README.md          # This file
```
