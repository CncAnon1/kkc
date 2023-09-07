import os
import argparse
import re
import asyncio
from typing import List
import aiohttp

BASE_API = "https://api.openai.com/v1"
RATE_LIMIT_PER_MODEL = {"gpt-3.5-turbo": 3500, "gpt-4": 200, "gpt-4-32k": 10}
oai_key_regex = re.compile(r"(sk-[a-zA-Z0-9]{20}T3BlbkFJ[a-zA-Z0-9]{20})")

# utils
def get_headers(key: str, org_id: str = None):
  headers = {"Authorization": f"Bearer {key}"}
  if org_id:
    headers["OpenAI-Organization"] = org_id
  return headers

class Key:
  def __init__(self, key_string: str, models: List[str] = []):
    self.key_string = key_string
    self.dead = False
    self.working = False
    self.trial_status = False
    self.over_quota = False
    self.org_name = ""
    self.org_id = ""
    self.org_default = False
    self.models = models
    self.ratelimit = 0

  def top_model(self) -> str:
    for model in reversed(RATE_LIMIT_PER_MODEL.keys()):
      if model in self.models:
        return model
    return ""

class KeyScanner:
  def __init__(self, keys: List[str], verbose: bool, max_requests = 20):
    self.keys = [match.group(1) for match in (oai_key_regex.search(key) for key in keys) if match]
    self.verbose = verbose
    self.file_handles = {model: open(f"scan_results/{model}.txt", "w") for model in RATE_LIMIT_PER_MODEL.keys()}
    self.file_handles["over_quota"] = open("scan_results/over_quota.txt", "w")
    self.sem = asyncio.Semaphore(max_requests)
    
    print(f"Total unique key count: {len(keys)}, starting the scan...")

  async def scan(self):
    tasks = [self.check_key(key) for key in self.keys]
    return await asyncio.gather(*tasks)

  async def check_key(self, key: str):
    result = []
    async with self.sem:
      if self.verbose:
        print("Checking key", key)
      
      orgs = await self.get_orgs(key)
      if not orgs:
        return
      
      for org in orgs:
        is_default_org = org["is_default"]
        if not is_default_org and self.verbose:
          print(f"Checking alternative org {org['name']} for {key}")
        
        models = await self.get_models(key, org["id"])
        # Not sure if this can happen, but just to be safe 
        if not models:
          continue
        
        status = Key(key, models=models)
        status.org_default = is_default_org
        status.org_name = org["name"]
        status.org_id = org["id"]

        top_model_name = status.top_model()
        await self.try_completion(status, top_model_name)
      
        if status.working or status.over_quota:
          self.write_key_to_file(status, top_model_name)
          result.append(status)
          if self.verbose:
            print(f"Good key {key} with model {top_model_name}")
      
      return result  

  async def get_models(self, key: str, org_id: str) -> List[str]:
    async with aiohttp.ClientSession() as session:
      async with session.get(f"{BASE_API}/models", headers=get_headers(key, org_id)) as resp:
        if resp.status != 200:
          return []
        data = await resp.json()
        result = [model["id"] for model in data["data"] if model["id"] in RATE_LIMIT_PER_MODEL]
        result.sort(key=lambda x: list(RATE_LIMIT_PER_MODEL.keys()).index(x))
        return result

  async def try_completion(self, status: Key, model: str):
    async with aiohttp.ClientSession() as session:
      req_data = {"model": model, "messages": [{"role": "user", "content": ""}], "max_tokens": -1}
      async with session.post(f"{BASE_API}/chat/completions", headers=get_headers(status.key_string, status.org_id), json=req_data) as resp:
        data = await resp.json()
        if resp.status == 401:
          # Just an invalid key
          return
        
        error_type = data.get("error", {}).get("type", "")
        if error_type in ["billing_not_active", "access_terminated"]:
          # Disabled or banned
          return
        elif error_type == "insufficient_quota":
          # Over quota
          status.over_quota = True
          return

        # Get the ratelimit for the top model
        ratelimit = int(resp.headers.get("x-ratelimit-limit-requests", "-1"))
        status.ratelimit = ratelimit
        # This really only gets triggered for turbo
        if ratelimit < RATE_LIMIT_PER_MODEL[model]:
          status.trial_status = True
        
        # If a key is overused by others but valid, it might be 
        # ratelimited when we're doing our request
        ratelimited = resp.status == 429
        if (resp.status == 400 and error_type == "invalid_request_error") or ratelimited:
          status.working = True
        return

  async def get_orgs(self, key: str):
    """
    Undocumented OpenAI API, "data" key is an array of org entries:
    {
      "object": "organization",
      "id": "org-<ORGID>",
      "created": 1685299576,
      "title": "<ORGTITLE>",
      "name": "<ORGNAME>",
      "description": null,
      "personal": false,
      "is_default": true,
      "role": "owner"
    }
    """
    url = "https://api.openai.com/v1/organizations"
    async with aiohttp.ClientSession() as session:
      async with session.get(url, headers=get_headers(key)) as resp:
        if resp.status != 200:
          return []
        data = await resp.json()
        return data["data"]

  def write_key_to_file(self, status: Key, top_model_name: str):
    outfile = self.file_handles["over_quota" if status.over_quota else top_model_name]
    output = status.key_string
    addons = []
    if not status.org_name.startswith("user-"):
      if status.org_default:
        addons.append(f"org '{status.org_name}'")
      else:
        addons.append(f"alternate org '{status.org_name}' with id '{status.org_id}'")

    if status.trial_status:
      addons.append("trial")
    if status.over_quota and "gpt-4" in top_model_name:
      addons.append(f"has {top_model_name}")
    if addons:
      output += " (" + ", ".join(addons) + ")"
    outfile.write(output + "\n")
    outfile.flush()

def main():
  parser = argparse.ArgumentParser(description="KKC - OpenAI key checker")
  parser.add_argument("file", help="Input file containing the keys")
  parser.add_argument("-v", "--verbose", action="store_true", help="Verbose scanning")
  parser.add_argument("-r", "--requests", type=int, default=20, help="Max number of requests to make at once")

  args = parser.parse_args()

  with open(args.file, "r") as f:
    keys = f.read().splitlines()

  keys = list(set(keys))

  if not os.path.exists("scan_results"):
    os.makedirs("scan_results")

  scanner = KeyScanner(keys, args.verbose, args.requests)
  # Each scan returns a list of 1 or more statuses (alternate orgs)
  good_keys = [key for key_result in asyncio.run(scanner.scan()) for key in key_result]

  # Initialize counters
  model_key_counts = {model: 0 for model in RATE_LIMIT_PER_MODEL.keys()}

  for key in good_keys:
    if not key or key.over_quota: continue
    top_model = key.top_model()
    model_key_counts[top_model] += 1
    print("---")
    print(f"{key.key_string}")
    for model in key.models:
      if model == top_model:
        ratelimit_text = str(key.ratelimit) if key.ratelimit >= 0 else "unknown"
        print(f"  - {model} (RPM: {ratelimit_text})")
      else:
        print(f"  - {model}")
    if key.trial_status:
      print("  - !trial key!")
    if not key.org_name.startswith("user-"):
      if key.org_default:
        print(f"Main org: {key.org_name}")
      else:
        print(f"Alternate org: {key.org_name} (id {key.org_id})")

    print("---\n")

  # Calculate total good keys
  total_good_keys = sum(model_key_counts.values())

  # Print total good keys and per model counts
  print(f"\nTotal good keys: {total_good_keys}")
  for model, count in model_key_counts.items():
    if count > 0:
      print(f"Number of good keys for {model}: {count}")

  for file in scanner.file_handles.values():
    file.close()

if __name__ == "__main__":
  main()