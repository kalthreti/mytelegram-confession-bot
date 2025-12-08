# Use a Python base image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /usr/src/app

# Copy the requirements file and install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application files
COPY . .

# Create a directory for persistent storage (the volume mount point)
RUN mkdir -p /data

# The final command to run the script
# Your bot will start polling here
CMD ["python", "confession_bot.py"]
