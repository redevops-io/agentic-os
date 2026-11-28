"""Create an API key in a THROWAWAY Twenty that was seeded with Twenty's own development workspace, and print the key (and nothing else) on
standard output. TWENTY_BASE is the instance. The sign-in is the sample user that Twenty's development seed creates and documents
(tim@apple.dev); an instance that holds real data has no such user, and this script then stops at the first step."""
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

B = os.environ["TWENTY_BASE"].rstrip("/")
SAMPLE_USER = "tim@apple.dev"   # Twenty's seed gives this user a password equal to the address


def gql(query, variables=None, token=None):
    h = {"Content-Type": "application/json", "Origin": B}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(B + "/metadata", data=json.dumps({"query": query, "variables": variables or {}}).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("data") or {}
    except urllib.error.HTTPError as e:
        sys.exit(f"Twenty answered {e.code} to a metadata request")


def need(value, step):
    if not value:
        sys.exit(f"could not {step} in the throwaway Twenty")
    return value


d = gql("mutation($e:String!,$p:String!,$o:String!){getLoginTokenFromCredentials(email:$e,password:$p,origin:$o){loginToken{token}}}",
        {"e": SAMPLE_USER, "p": SAMPLE_USER, "o": B})
login = need(((d.get("getLoginTokenFromCredentials") or {}).get("loginToken") or {}).get("token"), "sign in as the seeded sample user")
d = gql("mutation($t:String!,$o:String!){getAuthTokensFromLoginToken(loginToken:$t,origin:$o){tokens{accessOrWorkspaceAgnosticToken{token}}}}",
        {"t": login, "o": B})
access = need((((d.get("getAuthTokensFromLoginToken") or {}).get("tokens") or {}).get("accessOrWorkspaceAgnosticToken") or {}).get("token"), "exchange the login token")
expires = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
roles = gql("query{getRoles{id label}}", token=access).get("getRoles") or []
role = next((r["id"] for r in roles if r["label"] == "Admin"), None)
d = gql("mutation($i:CreateApiKeyInput!){createApiKey(input:$i){id}}",
        {"i": {"name": "integration-check", "expiresAt": expires, **({"roleId": role} if role else {})}}, token=access)
key_id = need((d.get("createApiKey") or {}).get("id"), "create an API key")
d = gql("mutation($id:UUID!,$e:String!){generateApiKeyToken(apiKeyId:$id,expiresAt:$e){token}}", {"id": key_id, "e": expires}, token=access)
print(need((d.get("generateApiKeyToken") or {}).get("token"), "generate the API key token"))
