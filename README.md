# Prism Telegram Bot - Render Hosting Guide

This version of the bot is optimized for **Render's Free Tier** with persistent storage via PostgreSQL and a keep-alive web server.

## Files in this Package
- `prism_bot.py`: Main bot code (PostgreSQL & Web Server enabled).
- `requirements.txt`: Python dependencies.
- `Dockerfile`: Container configuration.
- `render.yaml`: One-click deployment configuration.

## Deployment Steps

### 1. Prepare your GitHub Repository
1. Create a **Private** repository on GitHub (e.g., `prism-bot`).
2. Upload all the files in this package to that repository.

### 2. Deploy to Render
1. Sign up/Login to [Render.com](https://render.com).
2. Click **"New +"** and select **"Blueprint"**.
3. Connect your GitHub account and select your `prism-bot` repository.
4. Render will automatically detect the `render.yaml` file.
5. You will be prompted to enter your **`BOT_TOKEN`**:
   - Paste your token from @BotFather.
6. Click **"Apply"**. Render will now create:
   - A free PostgreSQL database.
   - A background worker for your bot.

### 3. Keep the Bot Awake (Optional but Recommended)
Since it's a free tier, Render might sleep the bot if it doesn't receive web traffic.
1. Once deployed, Render will give you a URL (e.g., `https://prism-bot.onrender.com`).
2. Go to [cron-job.org](https://cron-job.org) (Free).
3. Create a new cron job to "ping" your Render URL every 10 minutes. This ensures the bot stays online 24/7.

## Environment Variables
If you need to change settings later, go to the **Environment** tab in Render:
- `BOT_TOKEN`: Your Telegram Bot Token.
- `BTC_ADDRESS`: `bc1qdtngv6cwgh502pf726lef6632cza9d5s54x56m` (Already set in `render.yaml`).
- `DATABASE_URL`: Automatically linked from your Render Database.
