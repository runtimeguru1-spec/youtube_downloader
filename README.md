# Social Media Downloader

A powerful Python tool to download images, videos, and media content from multiple social media platforms including **Facebook**, **Instagram**, **YouTube**, and more.

---

## Features

- 📥 Download images and videos from Facebook posts  
- 📸 Download Instagram photos and videos using Instaloader  
- ▶️ Download YouTube videos with yt-dlp  
- 🤖 Automated browser control with Selenium and dynamic user agents  
- 🔧 Automatic ChromeDriver management via `webdriver-manager`  
- 🗂️ Saves media with timestamps in organized folders  

---

## Requirements

- Python 3.7 or higher  
- Google Chrome browser installed and updated  
- Internet connection  

---

## Installation

1. Clone this repository or download the source code.  
2. Install the required Python packages using:

   ```bash
   pip install -r requirements.txt
Usage
Run the main script and enter the social media post URL when prompted:
python social_media_downloader.py


Example input:
Enter post URL: https://www.facebook.com/photo/?fbid=123456789
Media files will be downloaded and saved inside appropriate folders, e.g., facebook_images/, instagram_media/, or youtube_videos/.


❗ Troubleshooting
Keep Google Chrome updated to avoid driver compatibility issues.

ChromeDriver is auto-managed but requires internet access for installation.

Confirm the post URL is valid and accessible.

If downloads fail, check your internet connection and retry.


🤝 Contributions
Contributions, bug reports, and feature requests are welcome! Feel free to open issues or submit pull requests.

# youtube_downloader
