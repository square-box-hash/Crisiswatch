import requests

URL = "https://crisiswatch-ohrl.onrender.com/outbreaks/detect"

payload = {
    "disease": "cholera",
    "region": "Lagos",
    "country": "Nigeria",
    "cases": 120,
    "deaths": 8,
    "population": 220000000,
    "trigger": "rainfall",
    "source_urls": []
}

try:
    res = requests.post(URL, json=payload, timeout=120)

    print("Status Code:", res.status_code)
    print("Response JSON:")
    print(res.json())

except Exception as e:
    print("Request failed:", str(e))