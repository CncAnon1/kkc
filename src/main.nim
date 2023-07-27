import std/[json, strutils, strformat, asyncdispatch, httpclient, net, sequtils, algorithm, tables, os, terminal, exitprocs]

const
  BaseApi = "https://api.openai.com/v1"

  # The default rate limits for a model
  # Helps spot rare keys that have higher limits 
  RateLimitPerModel = {
    "gpt-3.5-turbo": 3500,
    "gpt-4": 200,
    "gpt-4-32k": 10 # no actual clue, rare enough 
  }.toTable()

type
  KeyStatus = object
    key: string
    working: bool
    ratelimit: Table[string, int]
    models: seq[string]
    org: string

proc topModel(models: seq[string]): string = 
  if "gpt-4-32k" in models:
    "gpt-4-32k"
  elif "gpt-4" in models:
    "gpt-4"
  elif "gpt-3.5-turbo" in models:
    "gpt-3.5-turbo"
  else:
    ""

proc formatFileOut(key: KeyStatus): string = 
  result = &"{key.key}"
  var info: seq[string]

  if not key.org.startsWith("user-"):
    info.add &"org: {key.org}"
  
  let topModel = key.models.topModel()
  let curRatelimit = key.ratelimit[topModel]

  if curRatelimit > RateLimitPerModel[topModel]:
    info.add &"RPM: {curRatelimit}"
  elif curRatelimit < RateLimitPerModel[topModel]:
    info.add "trial"
  
  if info.len > 0:
    result &= " (" & info.join(", ") & ")"

proc getModels(key: string): Future[seq[string]] {.async.} = 
  let c = newAsyncHttpClient()
  c.headers["Authorization"] = "Bearer " & key
  try:
    let resp = await c.get(BaseApi & "/models")
    if resp.code != Http200:
      return
    let data = parseJson(await resp.body)
    for model in data["data"]:
      let modelId = model["id"].getStr()
      if modelId in RateLimitPerModel:
        result.add(modelId)
  finally:
    try:
      c.close()
    except:
      discard

proc tryCompletion(key: string, model: string): Future[KeyStatus] {.async.} = 
  let c = newAsyncHttpClient()
  c.headers["Authorization"] = "Bearer " & key
  c.headers["Content-Type"] = "application/json"
  var data = %*{
    "model": model,
    "messages": [{"role": "user", "content": ""}],
    "max_tokens": -1
  }
  try:
    let resp = await c.post(BaseApi & "/chat/completions", $data)
    let data = parseJson(await resp.body)
    if resp.code == Http401:
      # key isn't valid
      return

    let errorType = data{"error", "type"}.getStr()
    if errorType in ["insufficient_quota", "billing_not_active", "access_terminated"]:
      # owari da
      return
    # If the key is fully working we get 400, if it's working but ratelimited we get 429
    let ratelimited = resp.code == Http429
    if (resp.code == Http400 and errorType == "invalid_request_error") or ratelimited:
      result.ratelimit[model] = parseInt resp.headers.getOrDefault("x-ratelimit-limit-requests", @["0"].HttpHeaderValues)
      # Should be always present from what I've seen, but let's be safe
      result.org = resp.headers.getOrDefault("openai-organization", @["user-xyz"].HttpHeaderValues)
      result.working = true

  finally:
    try:
      c.close()
    except:
      discard

var
  currentReq = 0
  maxReq = 50

var keysPerModel = initTable[string, seq[KeyStatus]]()

proc checkKey(key: string) {.async.} = 
  inc currentReq
  try:
    # A model request only verifies that the key is _valid_, it doesn't verify
    # the key's quota. But we still use this as the first request because
    # we want to figure out the ratelimits only for the "top" model to not
    # do multiple completion requests
    let models = await getModels(key)
    if models.len > 0:
      let topModel = models.topModel
      var status = await tryCompletion(key, topModel)
      status.key = key
      status.models = models
      if status.working:
        keysPerModel.withValue(topModel, val):
          val[].add(status)
        do:
          keysPerModel[topModel] = @[status]
  except:
    echo getCurrentExceptionMsg()
    discard
  finally:
    dec currentReq

var readConsole = false
var outConsole = false

# Open the result files right away, so we don't fail later in case something is wrong
createDir("scan_results")

# Do the same for output files
var outFiles = {
  "gpt-3.5-turbo": open("scan_results" / "turbo.txt", fmWrite),
  "gpt-4": open("scan_results" / "gpt4.txt", fmWrite),
  "gpt-4-32k": open("scan_results" / "gpt4_32k.txt", fmWrite)
}.toTable()

proc writeResults = 
  for model, keys in keysPerModel:
    echo &"Total keys for {model}: {keys.len}"
    for key in keys:
      outFiles[model].writeLine(key.formatFileOut())

  for file in outFiles.values():
    file.flushFile()
    file.close()

proc main {.async.} = 
  copyDir("scan_results", "scan_results_old")
  var keys: seq[string]
  # Read keys from the file
  if not readConsole:
    if not fileExists("keys.txt"):
      quit("keys.txt not found, create it and put the keys to check there.")
    keys = readFile("keys.txt").splitLines()
  # Or from the terminal
  else:
    var line: string
    while stdin.readLine(line) and line != "":
      keys.add(line.strip())
  
  # Remove any duplicates
  keys = keys.deduplicate()
  echo &"Total unique key count: {keys.len}, starting scan..."

  # Scan all keys, clamping the max async requests to maxReq
  while keys.len > 0:
    while keys.len > 0 and currentReq < maxReq:
      let key = keys.pop()
      asyncCheck checkKey(key)
    await sleepAsync(50)
  
  # Wait for the leftover requests to finish
  while hasPendingOperations():
    await sleepAsync(500)
  
  # Terminal output
  var i = 0
  for model, keys in keysPerModel:
    for key in keys:
      echo fmt"API key {i+1}"
      echo key.key
      # OAI gives model results from the best to worst, we reverse that here
      for model in key.models.reversed():
        stdout.write(&"  - {model}")
        if model in key.ratelimit:
          let ratelimitDiff = key.ratelimit[model] - RateLimitPerModel[model]
          if ratelimitDiff < 0:
            stdout.styledWrite(&" (RPM: {key.ratelimit[model]} - trial)")
          elif ratelimitDiff > 0:
            stdout.styledWrite(&" (RPM: ", fgGreen, &"{key.ratelimit[model]}", resetStyle, "!)")
          else:
            stdout.write &" (RPM: {key.ratelimit[model]})"
        stdout.write "\n"
      if not key.org.startsWith("user-"):
        stdout.styledWrite(fgGreen, &"Organization (non-standard): {key.org}\n")
      echo ""
      inc i
  
  writeResults()
  echo "Saved results to scan_results"

addExitProc(resetAttributes)
waitFor main()