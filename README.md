# wbor-failsafe-notifier: Angry Audio Failsafe Gadget Notifier

**TL;DR: sends a Discord message when the backup audio source becomes active.**

WBOR uses Angry Audio's [Failsafe Gadget](https://angryaudio.com/failsafegadget/) to automatically switch to a backup audio source if our main mixing board/audio console stops emitting audio. If the audio level drops below -24db and remains there for 60 seconds, the unit will switch to the backup (B) source. In our case, this is a streaming computer that plays a loop of music. As soon as the audio console (our "A" source) resumes sending audio, the Failsafe Gadget will switch back to it.

Ideally, a member of station management should be notified when source B becomes active, as it indicates a failure with the audio console (since it stopped producing a signal). This is where some handy scripting comes in!

On the rear of the Failsafe Gadget is a DB9 logic port that can be used to monitor which source is currently active (amongst other things). Using a few jumper wires and a Raspberry Pi, we can read the logic port status in Python. In our case, we want to send a message to a Discord channel when the B source becomes active so that management can investigate the issue in person (and in a timely manner).

![Failsafe Gadget DB9 Pinout](/images/aa-pinout.png)

If you don't have a Pi with GPIO pins, you can also use a [FT232H USB to JTAG serial converter](https://amazon.com/dp/B09XTF7C1P), like we did, since our Pi is inside a case. Consequently, our code and instructions will be written with that in mind.

## Hardware

- **Raspberry Pi**: Pretty much any model will work, but we used a Pi 5 for this project since it was already in the studio running other apps.
- **[FT232H USB to JTAG serial converter](https://amazon.com/dp/B09XTF7C1P)**: Used in our case to read the logic port status from the Failsafe Gadget. You can also use a Raspberry Pi with GPIO pins if you prefer to go in directly, ***but may need to modify the code!***
- **[DB9 Breakout Connector](https://amazon.com/dp/B09L7JWNDQ)**: This is used to connect to the Failsafe Gadget's logic port.
- and finally, standard [breadboard jumper wires](https://amazon.com/dp/B07GD2BWPY) to make connections.

## Usage

This script was built using **[Python 3.13.2](https://www.python.org/downloads/)**. It is meant to operate as a systemd service, so it will run in the background and check the status of the Failsafe Gadget continually. If the backup source (B) becomes active, it will send a message to a Discord channel using a webhook.

1. Clone this repository to your device:

   ```bash
   git clone https://github.com/WBOR-91-1-FM/wbor-failsafe-notifier.git
   ```

2. Change into the directory:

   ```bash
   cd wbor-failsafe-notifier
   ```

3. Create a Python virtual environment:

   ```bash
   python3 -m venv venv
   ```

4. Activate the virtual environment:

   ```bash
   source venv/bin/activate
   ```

5. Install the required packages:

   ```bash
   pip install -r requirements.txt
   ```

6. Copy the sample .env `.env.sample` file to `.env`:

   ```bash
   cp .env.sample .env
   ```

7. Edit the `.env` file to include your Discord webhook URL and pin assignment. You can find instructions on how to create a Discord webhook [here](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks).

    ```bash
    nano .env
    ```

    Look for `PIN_ASSIGNMENT` and `DISCORD_WEBHOOK_URL` and update them accordingly. You may also choose to set the BACKUP_INPUT to `A` depending on your setup. The optional variables are provided to help customize the look and feel of the Discord message.

    Once you're done editing, save the file and exit the editor (in nano, press `CTRL + X`, then `Y`, then `ENTER`).

8. Run the script to test it:

   ```bash
   BLINKA_FT232H=1 python3 failsafe.py
   ```

   If everything is set up correctly, you should see a message in your Discord channel when the backup source (B) becomes active.

9. Set up the systemd service to run the script in the background:

    We have our script installed to `/home/pi5/Scripts/wbor-failsafe-notifier` (`pi5` is our Pi's username). ***If you installed it somewhere else, make sure to update the paths in the service file accordingly.*** Likewise, if your username is different, update the `User=` line in the service file.

    `Environment=BLINKA_FT232H=1` is required to use the FT232H USB to JTAG serial converter. If you remove this, the script will not work. If you are using a Raspberry Pi with GPIO pins, you can remove this line from the service file.

   ```bash
   sudo cp wbor-failsafe-notifier.service /etc/systemd/system/
   ```

10. Enable and start the service:

    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable wbor-failsafe-notifier.service
    sudo systemctl start wbor-failsafe-notifier.service
    ```

11. To verify that the service is running, you can check its status:

    ```bash
    sudo systemctl status wbor-failsafe-notifier.service
    ```

    You should see output indicating that the service is active and running.

    View/trail logs with:

    ```bash
    sudo journalctl -u wbor-failsafe-notifier.service -f
    ```

## References

- [Angry Audio Failsafe Gadget](https://angryaudio.com/failsafegadget/)
- [Failsafe Gadget Manual](https://angryaudio.com/wp-content/uploads/2022/08/AA_FailsafeGadgetUserGuide_2208031.pdf)
- [Discord Webhooks](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks)
- [Discord Webhook Embed Object](https://discord.com/developers/docs/resources/message#embed-object)
- [CircuitPython Libraries on any Computer with FT232H](https://learn.adafruit.com/circuitpython-on-any-computer-with-ft232h/)
