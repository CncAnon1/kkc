import os
import argparse
import re
import asyncio
from typing import List
import aiohttp

BASE_API = "https://api.openai.com/v1"
RATE_LIMIT_PER_MODEL = {"gpt-3.5-turbo": 3500, "gpt-4": 200, "gpt-4-32k": 10}
oai_key_regex = re.compile(r"(sk-[a-zA-Z0-9]{20}T3BlbkFJ[a-zA-Z0-9]{20})")

class Key:
  def __init__(self, key_string: str, models: List[str] = []):
    self.key_string = key_string
    self.dead = False
    self.working = False
    self.trial_status = False
    self.over_quota = False
    self.org = ""
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
    async with self.sem:
      if self.verbose:
        print("Checking key", key)
      
      models = await self.get_models(key)
      if not models:
        return

      status = Key(key, models=models)
      top_model_name = status.top_model()
      await self.try_completion(status, top_model_name)
      
      if status.working or status.over_quota:
        if status.over_quota:
          status.org = await self.get_org_name(key)
        self.write_key_to_file(status, top_model_name)
      return status

  async def get_models(self, key: str) -> List[str]:
    async with aiohttp.ClientSession() as session:
      async with session.get(f"{BASE_API}/models", headers={"Authorization": f"Bearer {key}"}) as resp:
        if resp.status != 200:
          return []
        data = await resp.json()
        result = [model["id"] for model in data["data"] if model["id"] in RATE_LIMIT_PER_MODEL]
        result.sort(key=lambda x: list(RATE_LIMIT_PER_MODEL.keys()).index(x))
        return result

  async def try_completion(self, status: Key, model: str):
    async with aiohttp.ClientSession() as session:
      req_data = {"model": model, "messages": [{"role": "user", "content": ""}], "max_tokens": -1}
      async with session.post(f"{BASE_API}/chat/completions", headers={"Authorization": f"Bearer {status.key_string}", "Content-Type": "application/json"}, json=req_data) as resp:
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
        ratelimit = int(resp.headers.get("x-ratelimit-limit-requests", "0"))
        status.ratelimit = ratelimit
        # This really only gets triggered for turbo
        if ratelimit < RATE_LIMIT_PER_MODEL[model]:
          status.trial_status = True
        
        # If a key is overused by others but valid, it might be 
        # ratelimited when we're doing our request
        ratelimited = resp.status == 429
        if (resp.status == 400 and error_type == "invalid_request_error") or ratelimited:
          status.org = resp.headers.get("openai-organization", "user-xyz")
          status.working = True
        return

  async def get_org_name(self, key: str) -> str:
    url = f"{BASE_API}/images/generations"
    async with aiohttp.ClientSession() as session:
      async with session.post(url, headers={"Authorization": f"Bearer {key}"}) as resp:
        return resp.headers.get("openai-organization", "user-xyz")

  def write_key_to_file(self, status: Key, top_model_name: str):
    outfile = self.file_handles["over_quota" if status.over_quota else top_model_name]
    output = status.key_string
    addons = []
    if not status.org.startswith("user-"):
      addons.append(f"org: {status.org}")
    if status.trial_status:
      addons.append("trial")
    if status.over_quota and "gpt-4" in top_model_name:
      addons.append(f"has {top_model_name}")
    if addons:
      output += " (" + ", ".join(addons) + ")"
    outfile.write(output + "\n")
    outfile.flush()

def main():
  parser = argparse.ArgumentParser(description="Kute Key Checker")
  parser.add_argument("file", help="file containing the keys")
  parser.add_argument("-v", "--verbose", action="store_true", help="show the keys being currently checked")
  parser.add_argument("-r", "--requests", type=int, default=20, help="Max number of requests to make at once")

  args = parser.parse_args()

  with open(args.file, "r") as f:
    keys = f.read().splitlines()

  keys = list(set(keys))

  if not os.path.exists("scan_results"):
    os.makedirs("scan_results")
  if not os.path.exists("scan_results_quota"):
    os.makedirs("scan_results_quota")

  scanner = KeyScanner(keys, args.verbose, args.requests)
  good_keys = asyncio.run(scanner.scan())

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
        print(f"  - {model} (RPM: {key.ratelimit})")
      else:
        print(f"  - {model}")
    if key.trial_status:
      print("  - !trial key!")
    if not key.org.startswith("user-"):
      print(f"Organization (unique): {key.org}")
    print("")

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