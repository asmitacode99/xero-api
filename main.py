import os
import json
import requests
import time
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from xero_python.accounting import AccountingApi, Contact as XeroContact, Phone
from xero_python.api_client import ApiClient
from xero_python.api_client.oauth2 import OAuth2Token
from pydantic import BaseModel
from dotenv import load_dotenv
from xero_python.api_client.configuration import Configuration

# Load environment variables from .env file
load_dotenv()

# FastAPI app
app = FastAPI()

# Xero credentials from environment variables
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

# OAuth2 URLs
TOKEN_URL = "https://identity.xero.com/connect/token"
AUTHORIZATION_URL = (
    f"https://login.xero.com/identity/connect/authorize"
    f"?response_type=code&client_id={CLIENT_ID}"
    f"&redirect_uri={REDIRECT_URI}&scope=openid profile email accounting.transactions accounting.contacts offline_access"
)

# Load or initialize the tokens
tokens = {}

### ---- TOKEN MANAGEMENT ---- ###

def load_tokens():
    """Load tokens from a JSON file."""
    try:
        with open('tokens.json', 'r') as f:
            tokens_data = json.load(f)
            print("Tokens loaded:", tokens_data)  # Add this line to check the loaded tokens
            return tokens_data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_tokens():
    """Save tokens to a JSON file."""
    with open('tokens.json', 'w') as file:
        json.dump(tokens, file)

def is_token_expired():
    """Check if the current access token is expired."""
    print("Checking if token is expired...", time.time(), tokens.get("expires_in", 0))
    return time.time() > tokens.get("expires_in", 0)

def refresh_access_token():
    """Refreshes the access token using the refresh token."""
    if "refresh_token" not in tokens:
        raise ValueError("No refresh token found.")

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": tokens["refresh_token"]
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    if response.status_code == 200:
        new_tokens = response.json()
        tokens.update({
            "access_token": new_tokens["access_token"],
            "refresh_token": new_tokens["refresh_token"],
            "expires_in": time.time() + new_tokens["expires_in"],
        })
        save_tokens()  # Save updated tokens
        return tokens["access_token"]  # Return the new access token
    else:
        raise Exception(f"Failed to refresh access token: {response.json()}")
    

def get_api_client():
    """Returns an initialized ApiClient with OAuth2 authentication."""
    global tokens

    if not tokens.get("access_token"):
        raise ValueError("No valid access token found.")
    if not tokens.get("tenant_id"):
        raise ValueError("No valid tenant ID found.")

    if is_token_expired():
        print("Token expired, refreshing...")
        tokens["access_token"] = refresh_access_token()

    # Ensure the access token is set
    if not tokens["access_token"]:
        raise ValueError("Failed to set access token after refresh.")

    # Create Configuration
    configuration = Configuration()
    configuration.access_token = tokens["access_token"]

    # Initialize ApiClient
    api_client = ApiClient(configuration)

    return api_client



### ---- ROUTES ---- ###

@app.get("/authorize")
async def authorize():
    """Redirects user to Xero authorization page."""
    return RedirectResponse(url=AUTHORIZATION_URL)


@app.get("/callback")
async def callback(request: Request):
    """Handles OAuth2 callback and stores tokens."""
    code = request.query_params.get("code")
    if not code:
        return {"error": "Authorization failed, no code received"}

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "redirect_uri": REDIRECT_URI
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    if response.status_code == 200:
        response_data = response.json()
        access_token = response_data["access_token"]
        refresh_token = response_data["refresh_token"]

        tenant_response = requests.get(
            "https://api.xero.com/connections",
            headers={"Authorization": f"Bearer {access_token}"}
        )

        tenant_data = tenant_response.json()
        if not tenant_data:
            return {"error": "No tenant found"}

        tokens.update({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "tenant_id": tenant_data[0]["tenantId"],
            "expires_in": time.time() + response_data["expires_in"],
        })

        save_tokens()  # Save the tokens in the file
        return {"message": "Authorization successful", "access_token": access_token}
    else:
        return {"error": f"Failed to exchange token: {response.json()}"}


@app.get("/contacts")
def get_contacts():
    """Fetches a list of contacts from Xero."""
    try:
        api_client = get_api_client()
        accounting_api = AccountingApi(api_client)

        # Fetch contacts
        contacts_response = accounting_api.get_contacts(xero_tenant_id=tokens["tenant_id"])

        # Process contacts
        contact_list = [
            {
                "contact_id": contact.contact_id,
                "name": contact.name,
                "email": contact.email_address,
                "phone_number": contact.phones[0].phone_number if contact.phones else None
            }
            for contact in contacts_response.contacts
        ]
        return {"message": "Contacts fetched successfully", "contacts": contact_list}
    except Exception as e:
        return {"error": f"Error fetching contacts: {str(e)}"}


class ContactData(BaseModel):
    name: str
    email: str = None
    phone_number: str = None
    first_name: str = None
    last_name: str = None
    contact_status: str = "ACTIVE"


@app.post("/create_contact")
async def create_contact(contact_data: ContactData):
    """Creates a new contact in Xero."""
    try:
        api_client = get_api_client()
        accounting_api = AccountingApi(api_client)

        xero_contact = XeroContact(
            name=contact_data.name,
            email_address=contact_data.email or "",
            phones=[Phone(phone_number=contact_data.phone_number or "")] if contact_data.phone_number else [],
            first_name=contact_data.first_name or "",
            last_name=contact_data.last_name or "",
            contact_status=contact_data.contact_status
        )

        created_contacts = accounting_api.create_contacts(
            xero_tenant_id=tokens["tenant_id"], 
            contacts=[xero_contact]
        )
        created_contact = created_contacts.contacts[0] if created_contacts.contacts else None

        return {"message": "Contact created successfully", "contact": created_contact.to_dict()} if created_contact else {"error": "No contact returned"}
    except Exception as e:
        return {"error": f"Error creating contact: {str(e)}"}