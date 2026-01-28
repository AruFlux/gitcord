# GitCord Bot 

A Discord bot that can create, edit, and manage GitHub repositories directly from Discord.

## Features
- Create new GitHub repositories
- Create/edit files with syntax highlighting
- Automatic repo switching
- List repository contents
- View file contents
- Secure file operations

## Commands
- `/repo [name]` - Switch to/create a repository
- `/create [filename] [content]` - Create a new file
- `/edit [filename] [content]` - Edit an existing file
- `/view [filename]` - View file content
- `/list` - List repository files
- `/current` - Show current repository

## Railway Deployment
1. Push this repo to GitHub
2. Connect to Railway.app
3. Add environment variables in Railway dashboard
4. Deploy!

## Environment Variables
- `DISCORD_TOKEN` - Your Discord bot token
- `GITHUB_TOKEN` - GitHub Personal Access Token (repo scope)
- `GITHUB_USERNAME` - Your GitHub username
- `DEFAULT_REPO` - Default repository name (optional)
