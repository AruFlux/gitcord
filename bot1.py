# bot.py
import os
import asyncio
import random
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands
import aiohttp


# --- Constants ---
MANGADEX_API_BASE = "https://api.mangadex.org"
CONTENT_RATINGS = ["safe", "suggestive"]  # 16+ only
STATUS_OPTIONS = ["ongoing", "completed", "hiatus", "cancelled"]
SORT_OPTIONS = {
    "Rating": "rating",
    "Popularity": "followedCount",
    "Year (Newest)": "year",
    "Year (Oldest)": "year",
    "Latest Chapter": "latestUploadedChapter"
}


# --- Data Classes ---
@dataclass
class MangaData:
    """Container for parsed manga data."""
    id: str
    title: str
    description: Optional[str]
    year: Optional[int]
    status: str
    tags: List[Dict[str, Any]]
    cover_filename: Optional[str]
    
    @property
    def cover_url(self) -> Optional[str]:
        if self.cover_filename:
            return f"https://uploads.mangadex.org/covers/{self.id}/{self.cover_filename}.256.jpg"
        return None
    
    @property
    def mangadex_url(self) -> str:
        return f"https://mangadex.org/title/{self.id}"


# --- API Client ---
class MangaDexAPI:
    """Handles all MangaDex API interactions."""
    
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.base_url = MANGADEX_API_BASE
    
    async def fetch_tags(self) -> List[Dict[str, Any]]:
        """Fetch available manga tags from MangaDex."""
        try:
            async with self.session.get(f"{self.base_url}/manga/tag") as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("data", [])
                return []
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return []
    
    async def search_manga(self, **params) -> List[MangaData]:
        """Search manga with given parameters."""
        url = f"{self.base_url}/manga"
        
        # Enforce content rating filter
        if "contentRating[]" not in params:
            params["contentRating[]"] = CONTENT_RATINGS
        
        try:
            async with self.session.get(url, params=params, timeout=10) as response:
                if response.status != 200:
                    return []
                
                data = await response.json()
                return await self._parse_manga_list(data.get("data", []))
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return []
    
    async def _parse_manga_list(self, manga_list: List[Dict]) -> List[MangaData]:
        """Parse raw API response into MangaData objects."""
        results = []
        
        for manga in manga_list:
            try:
                attrs = manga.get("attributes", {})
                
                # Get title (prefer English)
                title_dict = attrs.get("title", {})
                title = (
                    title_dict.get("en") or
                    next(iter(title_dict.values()), "Unknown Title")
                )
                
                # Get description
                desc_dict = attrs.get("description", {})
                description = (
                    desc_dict.get("en") or
                    next(iter(desc_dict.values()), None)
                )
                
                # Clean description
                if description:
                    description = description.replace("\n", " ").strip()
                
                # Get year
                year = None
                pub_date = attrs.get("year")
                if pub_date:
                    try:
                        year = int(pub_date)
                    except (ValueError, TypeError):
                        pass
                
                # Get status
                status = attrs.get("status", "unknown")
                
                # Get tags (limit to 10)
                tags = []
                for rel in manga.get("relationships", []):
                    if rel.get("type") == "tag":
                        tag_attrs = rel.get("attributes", {})
                        name = tag_attrs.get("name", {}).get("en", "Unknown")
                        group = tag_attrs.get("group", "unknown")
                        tags.append({"name": name, "group": group})
                        if len(tags) >= 10:
                            break
                
                # Get cover filename
                cover_filename = None
                for rel in manga.get("relationships", []):
                    if rel.get("type") == "cover_art":
                        cover_filename = rel.get("attributes", {}).get("fileName")
                        break
                
                results.append(MangaData(
                    id=manga["id"],
                    title=title,
                    description=description,
                    year=year,
                    status=status,
                    tags=tags,
                    cover_filename=cover_filename
                ))
            except (KeyError, AttributeError):
                continue
        
        return results


# --- Discord UI Components ---
class GenreSelect(discord.ui.Select):
    def __init__(self, tags: List[Dict[str, Any]]):
        options = []
        for tag in tags[:25]:  # Discord limit
            name = tag["attributes"]["name"]["en"][:100]  # Truncate if needed
            group = tag["attributes"]["group"]
            options.append(
                discord.SelectOption(
                    label=name[:25],
                    value=tag["id"],
                    description=f"{group.capitalize()}"[:50]
                )
            )
        
        super().__init__(
            placeholder="Select genres (optional)",
            min_values=0,
            max_values=5,
            options=options,
            row=0
        )
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class StatusSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Ongoing", value="ongoing", emoji="🟢"),
            discord.SelectOption(label="Completed", value="completed", emoji="✅"),
            discord.SelectOption(label="Hiatus", value="hiatus", emoji="🟡"),
            discord.SelectOption(label="Cancelled", value="cancelled", emoji="🔴"),
        ]
        
        super().__init__(
            placeholder="Status (optional)",
            min_values=0,
            max_values=1,
            options=options,
            row=1
        )
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class ContentRatingSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Safe (All Ages)", value="safe", emoji="🛡️"),
            discord.SelectOption(label="Suggestive (16+)", value="suggestive", emoji="⚠️"),
        ]
        
        super().__init__(
            placeholder="Content Rating (optional)",
            min_values=0,
            max_values=1,
            options=options,
            row=1
        )
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class LanguageSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Japanese", value="ja", emoji="🇯🇵"),
            discord.SelectOption(label="English", value="en", emoji="🇺🇸"),
            discord.SelectOption(label="Korean", value="ko", emoji="🇰🇷"),
            discord.SelectOption(label="Chinese", value="zh", emoji="🇨🇳"),
            discord.SelectOption(label="Multiple", value="mul", emoji="🌍"),
        ]
        
        super().__init__(
            placeholder="Language (optional)",
            min_values=0,
            max_values=1,
            options=options,
            row=2
        )
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class SortSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Rating", value="rating", emoji="⭐"),
            discord.SelectOption(label="Popularity", value="followedCount", emoji="🔥"),
            discord.SelectOption(label="Year (Newest)", value="-year", emoji="⬇️"),
            discord.SelectOption(label="Year (Oldest)", value="year", emoji="⬆️"),
            discord.SelectOption(label="Latest Chapter", value="-latestUploadedChapter", emoji="🆕"),
        ]
        
        super().__init__(
            placeholder="Sort by...",
            min_values=1,
            max_values=1,
            options=options,
            row=2
        )
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class RecommendationView(discord.ui.View):
    """Main view containing all selection components."""
    
    def __init__(self, api_client: MangaDexAPI, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.api = api_client
        self.selected_genres = []
        self.selected_status = None
        self.selected_rating = None
        self.selected_language = None
        self.selected_sort = "-rating"  # Default: rating descending
    
    async def setup(self):
        """Setup the view with genre options fetched from API."""
        tags = await self.api.fetch_tags()
        
        # Add all select components
        self.add_item(GenreSelect(tags))
        self.add_item(StatusSelect())
        self.add_item(ContentRatingSelect())
        self.add_item(LanguageSelect())
        self.add_item(SortSelect())
    
    def _build_api_params(self) -> Dict[str, Any]:
        """Build API parameters from user selections."""
        params = {}
        
        # Genres
        if self.selected_genres:
            params["includedTags[]"] = self.selected_genres
        
        # Status
        if self.selected_status:
            params["status[]"] = [self.selected_status]
        
        # Content Rating
        if self.selected_rating:
            params["contentRating[]"] = [self.selected_rating]
        else:
            params["contentRating[]"] = CONTENT_RATINGS
        
        # Language
        if self.selected_language:
            params["originalLanguage[]"] = [self.selected_language]
        
        # Sort order
        if self.selected_sort.startswith("-"):
            sort_field = self.selected_sort[1:]
            params[f"order[{sort_field}]"] = "desc"
        else:
            params[f"order[{self.selected_sort}]"] = "asc"
        
        # Limit and offset for randomness
        params["limit"] = "10"
        params["offset"] = str(random.randint(0, 20))
        
        return params
    
    @discord.ui.button(label="🎯 Get Recommendation", style=discord.ButtonStyle.primary, row=3)
    async def submit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle submission of all selections."""
        await interaction.response.defer(thinking=True)
        
        # Update selections from UI components
        for child in self.children:
            if isinstance(child, discord.ui.Select):
                if isinstance(child, GenreSelect):
                    self.selected_genres = child.values
                elif isinstance(child, StatusSelect) and child.values:
                    self.selected_status = child.values[0]
                elif isinstance(child, ContentRatingSelect) and child.values:
                    self.selected_rating = child.values[0]
                elif isinstance(child, LanguageSelect) and child.values:
                    self.selected_language = child.values[0]
                elif isinstance(child, SortSelect) and child.values:
                    self.selected_sort = child.values[0]
        
        # Build and execute API request
        params = self._build_api_params()
        manga_list = await self.api.search_manga(**params)
        
        # Handle results
        if not manga_list:
            embed = discord.Embed(
                title="No Results Found",
                description="Try adjusting your filters or selecting fewer genres.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Select random manga from results
        manga = random.choice(manga_list[:5])
        
        # Create and send embed
        embed = self._create_manga_embed(manga)
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="📖 Read on MangaDex",
            url=manga.mangadex_url,
            style=discord.ButtonStyle.link
        ))
        
        await interaction.followup.send(embed=embed, view=view)
    
    def _create_manga_embed(self, manga: MangaData) -> discord.Embed:
        """Create a Discord embed for manga display."""
        embed = discord.Embed(
            title=manga.title[:256],
            description=self._trim_description(manga.description),
            color=discord.Color.blue(),
            url=manga.mangadex_url
        )
        
        # Add thumbnail if available
        if manga.cover_url:
            embed.set_thumbnail(url=manga.cover_url)
        
        # Add metadata fields
        if manga.year:
            embed.add_field(name="📅 Year", value=str(manga.year), inline=True)
        
        embed.add_field(
            name="📊 Status", 
            value=manga.status.capitalize(), 
            inline=True
        )
        
        # Add tags (limit to 5)
        if manga.tags:
            tags_display = ", ".join([tag["name"] for tag in manga.tags[:5]])
            if tags_display:
                embed.add_field(
                    name="🏷️ Genres", 
                    value=tags_display[:1024], 
                    inline=False
                )
        
        embed.set_footer(text="Powered by MangaDex API • Click the button to read!")
        
        return embed
    
    def _trim_description(self, description: Optional[str]) -> str:
        """Trim description to safe length."""
        if not description:
            return "No description available."
        
        # Remove excessive whitespace
        description = " ".join(description.split())
        
        if len(description) > 500:
            return description[:497] + "..."
        
        return description


# --- Bot Setup ---
class MangaBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(
            command_prefix="!",  # Not used, but required
            intents=intents,
            help_command=None
        )
        
        self.api_session = None
    
    async def setup_hook(self):
        """Initialize the bot."""
        # Create aiohttp session
        self.api_session = aiohttp.ClientSession()
        
        # Sync commands
        try:
            await self.tree.sync()
            print("✅ Slash commands synced successfully")
        except Exception as e:
            print(f"⚠️ Failed to sync commands: {e}")
    
    async def close(self):
        """Cleanup on bot shutdown."""
        if self.api_session:
            await self.api_session.close()
        await super().close()
    
    async def on_ready(self):
        """Bot startup handler."""
        print(f'✅ Logged in as {self.user} (ID: {self.user.id})')
        print('------')


# --- Bot Instance and Command ---
bot = MangaBot()


@bot.tree.command(name="recommend", description="Get personalized manga recommendations")
@app_commands.guild_only()
async def recommend_command(interaction: discord.Interaction):
    """Main recommendation slash command."""
    await interaction.response.defer(thinking=True)
    
    # Create API client
    api_client = MangaDexAPI(bot.api_session)
    
    # Create and setup view
    view = RecommendationView(api_client)
    await view.setup()
    
    # Create initial embed
    embed = discord.Embed(
        title="🎯 Manga Recommender",
        description="Select your preferences below and click **Get Recommendation**.",
        color=discord.Color.green()
    )
    embed.add_field(
        name="📝 How to use:",
        value="1. Select genres (optional, up to 5)\n"
              "2. Choose status, rating, language (optional)\n"
              "3. Pick sort order\n"
              "4. Click **Get Recommendation**",
        inline=False
    )
    embed.add_field(
        name="💡 Tip:",
        value="Filters are optional. Leaving them blank gives more variety.",
        inline=False
    )
    embed.set_footer(text="Select options and click the button below")
    
    await interaction.followup.send(embed=embed, view=view)


# --- Error Handling ---
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Handle slash command errors."""
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⚠️ Please wait {error.retry_after:.1f} seconds before using this command again.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "❌ An error occurred while processing your command.",
            ephemeral=True
        )
        print(f"Command error: {error}")


# --- Entry Point ---
if __name__ == "__main__":
    token = os.getenv("D_TOKEN")
    if not token:
        raise ValueError("D_TOKEN environment variable not set")
    
    bot.run(token)
