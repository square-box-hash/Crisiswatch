from worker import register_worker, verify_worker, submit_worker_message
from db import outbreaks

# Get the most recent Ebola outbreak ID
ebola = outbreaks.find_one(
    {"disease": "Ebola"},
    sort=[("created_at", -1)]
)
outbreak_id = str(ebola["_id"])
print(f"Testing with outbreak: {outbreak_id}")

# Register a health worker
worker_id = register_worker({
    "name": "Dr. Amara Diallo",
    "credentials": "MD, Infectious Disease Specialist",
    "license_number": "DRC-MED-4821",
    "country": "DRC",
    "institution": "Kampala Regional Hospital",
    "specialty": "Infectious Disease"
})

# Verify them (admin action)
verify_worker(worker_id)

# Submit their review
submit_worker_message(
    outbreak_id=outbreak_id,
    worker_id=worker_id,
    message="Confirmed Ebola cases in North Kivu. Transmission is contact-based, not airborne. Local health teams deployed. This strain matches 2018 Kivu outbreak pattern but appearing outside known endemic zones — treating as novel.",
    classification_vote="novel",
    severity_vote="critical"
)