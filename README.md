## KKC

- Install Python
- `pip install -r requirements.txt`
- `python main.py keys.txt` (assuming you have keys.txt with the keys in the same folder)

If you want to see the key scanning progress, run the program with `-v`, like `python main.py -v keys.txt`.

You can also change the amount of simultaneous requests done by the script by using the `-r` or `--requests` option. For example, to limit the requests to 10 at a time, use `python main.py -r 10 keys.txt`. The default is `20`.

### Output
The checker will output keys both to the console and into the files. The `scan_results` folder will contain files with the keys for the model, and all over-quota keys will go to the `scan_results/over_quota.txt` file.

### Features
- Asynchronous - multiple key checks are done at the same time
- Ratelimits - allows to see if a key is a trial one, or has higher than default ratelimits, which usually means better quota
- Organizations - fetches the list of all organizations (including their names) a key can be used with, and checks each organization's status separately. If an organization is not a default one, it usually means better quota

### Changelog
- 2023/09/07
  - Improved organization handling, now the key checker will try the key with all of the organizations assigned to it, and will show the status of each organization separately.
  - When a ratelimit wasn't received in the completion response, it'll properly show up as unknown instead of 0.