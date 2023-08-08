## KuteKeyChecker

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
- Organization names - will show you the key's organization owner if it's not the default "user-" name, which can also hint at better quota. NEW: Even works for over-quota keys!