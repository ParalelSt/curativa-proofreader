# Curativa Proofreading

A local tool for proofreading speech clips. It downloads a recording from a YouTube link, cuts it into clips with a draft text, and lets you correct each clip. Finished recordings are packaged into one zip to send.

Everything runs on your own computer. Your work is saved in the `data` folder next to the tool.

## Start

**Windows:** double-click `Start.bat`.
**Mac:** double-click `start.command`.

You need Python 3.11 or newer. The first start takes a minute while the tool installs what it needs. Then it opens in your browser at http://127.0.0.1:8765. Keep the black window open while you work.

## How to use it

1. **Key (first time only).** Click **+ Add recordings** and paste the ElevenLabs key you were given. It is saved on this computer only.
2. **Add recordings.** Copy the YouTube links of the sheet items you want and paste them, one per line. Click **Check links**, then **Add**. Items that are already taken or done in the sheet are skipped. Each recording takes a few minutes; you can keep working meanwhile.
3. **Mark them in the sheet.** Set the status of the items you took to `u tijeku` and add your name.
4. **Proofread.** Click a recording on the left and listen to each clip. Fix the text so it is exactly what is said: keep every "eh" and "hm" and every repeated word, and write numbers as digits. Then click **Approve and next**, or **Exclude from training** if the clip is unusable. Your work saves automatically.
5. **Mark as done.** When every clip of a recording is decided, click **Mark as done**.
6. **Send your work.** Click **Package finished work**. A zip appears in the `outbox` folder inside the tool folder. Send that zip, and set the sheet status to `da`.

**Status list** shows every sheet item, who has it, and its state in your tool.

## Receiving someone else's work

Click **Import finished work**, choose the zip files you received and click **Import**. Recordings you already have are skipped, so your own work is never overwritten.

## Problems

- **"Windows protected your PC":** click **More info**, then **Run anyway**.
- **"No internet connection":** check the connection and click **Try again**.
- **"The ElevenLabs key was not accepted":** ask for a new key.
- **Anything else:** send the file `data/tool.log`.

To stop the tool, close the black window.
