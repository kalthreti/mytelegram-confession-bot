# Use a lightweight Python base image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /usr/src/app

# Copy the requirements file and install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application files (confession_bot.py, etc.)
COPY . .

# Create the /data directory, which will be the mount point for the persistent volume.
# Railway's Storage will attach to this folder.
RUN mkdir -p /data

# The final command to run the script. This starts the bot in polling mode.
CMD ["python", "confession_bot.py"]
