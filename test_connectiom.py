from pymongo import MongoClient
from dotenv import load_dotenv
import os
from datetime import datetime, timedelta, timezone

load_dotenv()
client = MongoClient(os.getenv("MONGODB_URI"))

try:
    client.admin.command('ping')
    print("Connected successfully")
except Exception as e:
    print(f"Failed: {e}")