# wbor-failsafe-notifier: Angry Audio Failsafe Gadget Notifier

**TL;DR: sends notifications via Discord, GroupMe, Email, and optionally RabbitMQ when the backup audio source becomes active.**

WBOR uses Angry Audio's [Failsafe Gadget](https://angryaudio.com/failsafegadget/) to automatically switch to a backup audio source if our main mixing board/audio console stops emitting audio. If the audio level drops below -24db and remains there for 60 seconds, the unit will switch to the backup (B) source. In our case, this is a streaming computer that plays a loop of music. As soon as the audio console (our "A" source) resumes sending audio, the Failsafe Gadget will switch back to it.

Ideally, a member of station management should be notified when source B becomes active, as it indicates a failure with the audio console (since it stopped producing a signal). This is where some handy scripting comes in!

On the rear of the Failsafe Gadget is a DB9 logic port that can be used to monitor which source is currently active (amongst other things). Using a few jumper wires and an ARM single-board computer (e.g. a Raspberry Pi), we can read the logic port status in Python. In our case, we want to send a message to some destination (such as: a Discord channel, GroupMe group, or RabbitMQ exchange) when the B source becomes active so that management can investigate the issue in person (and in a timely manner).

![Failsafe Gadget DB9 Pinout](/images/aa-pinout.png)

If you don't have direct access to GPIO pins, you can also use a [FT232H USB to JTAG serial converter](https://amazon.com/dp/B09XTF7C1P), like we did, since our board is inside a case. Consequently, our code and instructions will be written with that in mind.

## Notification Options

The script is written to suit **our needs**, but you can easily modify it to suit your own. By default, it will follow the following logic:

* If the backup source (B) becomes active, we query our [Spinitron API proxy](https://github.com/WBOR-91-1-FM/spinitron-proxy/) to get information about the current playlist and on-air DJ. If this information is available, and we're not currently broadcasting an automation (unattended) playlist, we bundle up the info into a Discord embed that is sent to our `#tech-ops` channel so that station technical staff are made aware of the issue.
  * If the current playlist is NOT automated and we are unable to fetch the email address of the current DJ, we fall back to sending a message to **ALL DJs** in the DJ-wide GroupMe group. This is done to ensure that *someone* is made aware of the issue, even if the DJ's email address is not available.
* Simultaneously, we send a message to the GroupMe group with the same information to the management-wide GroupMe group (that includes non-technical staff members).
* If an email address was found, we send the DJ an email letting them know that the backup source is active and they should check board's status. This is done using the [smtplib](https://docs.python.org/3/library/smtplib.html) library in Python. The email is sent from the address specified in the `.env` file.
  * Any time an email is sent, we notify the tech-ops channel in Discord letting them know that an email was sent to the DJ.
* **Optionally**, all source change events can be published to a RabbitMQ message queue for consumption by other services (e.g., centralized logging, further analytics, or custom alerting systems).

## Installation & Setup

### Prerequisites

* **ARM Single-Board Computer** (e.g., Raspberry Pi) with Python 3.9+
* **[FT232H USB to JTAG serial converter](https://amazon.com/dp/B09XTF7C1P)** or direct GPIO access
* **[DB9 Breakout Connector](https://amazon.com/dp/B09L7JWNDQ)** and [jumper wires](https://amazon.com/dp/B07GD2BWPY)

### Quick Start

1. **Clone and setup:**

   ```bash
   git clone https://github.com/WBOR-91-1-FM/wbor-failsafe-notifier.git
   cd wbor-failsafe-notifier
   ```

2. **Install dependencies:**
   You can use either `uv` (recommended, [installation instructions](https://docs.astral.sh/uv/getting-started/installation/)) or `pip` to manage dependencies.

   ```bash
   # Using uv (recommended)
   uv sync
   source .venv/bin/activate
   
   # Or using pip
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure environment:**

   ```bash
   cp .env.sample .env
   nano .env  # Edit configuration (see Configuration section below)

   # or using Makefile
   make env-copy
   nano .env
   ```

4. **Test the setup:**

   ```bash
   BLINKA_FT232H=1 python3 failsafe.py
   ```

### Configuration

Edit `.env` to configure your setup. Key sections:

**Required Settings:**

* `PIN_ASSIGNMENT` - GPIO pin (e.g., `D7` for FT232H, `D17` for direct GPIO)
* `BACKUP_INPUT` - Which input is backup (`A` or `B`)

**Notification Services (at least one required):**

* `DISCORD_WEBHOOK_URL` - Discord webhook for alerts
* `GROUPME_BOT_ID_MGMT`, `GROUPME_BOT_ID_DJS` - GroupMe bot IDs
* Email settings (`SMTP_SERVER`, `SMTP_USERNAME`, etc.)
* RabbitMQ settings for message queue integration

**Optional Settings:**

* `TIMEZONE` - Display timezone (default: `America/New_York`)
* `SPINITRON_API_BASE_URL` - For fetching current DJ/playlist info

See `.env.sample` for complete configuration details and examples.

### Production Deployment

1. **Install systemd service:**
Edit paths in service file if needed

   ```bash
   sudo cp wbor-failsafe-notifier.service /etc/systemd/system/
   sudo systemctl daemon-reload

   # or using the Makefile
   make service-install
   ```

2. **Enable and start:**

   ```bash
   sudo systemctl enable wbor-failsafe-notifier.service
   sudo systemctl start wbor-failsafe-notifier.service

   # or using the Makefile

   make service-enable && make service-start
   ```

3. **Monitor service:**

   ```bash
   # Check status
   sudo systemctl status wbor-failsafe-notifier.service
   
   # View logs
   sudo journalctl -u wbor-failsafe-notifier.service -f

   # or using the Makefile
   make service-status
   make service-logs
   ```

### Updates

```bash
cd wbor-failsafe-notifier
git pull origin main
sudo systemctl restart wbor-failsafe-notifier.service
```

## Development

### Development Setup

1. **Clone and install dev dependencies:**

   ```bash
   git clone https://github.com/WBOR-91-1-FM/wbor-failsafe-notifier.git
   cd wbor-failsafe-notifier
   
   # Using uv (recommended)
   uv sync --dev
   
   # Or using pip
   python3 -m venv venv && source venv/bin/activate
   pip install -e .[dev]
   ```

2. **Configure environment:**
Edit `.env` with your configuration.

   ```bash
   cp .env.sample .env # or make env-copy
   ```

### Code Quality and Linting

This project uses [Ruff](https://ruff.rs/) for linting and code formatting, and [mypy](https://mypy-lang.org/) for static type checking. The configuration follows modern Python standards with Google-style docstrings.

#### Using Make targets

```bash
# Format code automatically
make format

# Run linting checks
make lint

# Run type checking
make typecheck

# Format code automatically with potentially unsafe fixes
make lint-unsafe-fix

# Run both formatting, linting, and type checking
make check
```

### Running in Development

```bash
# Using uv
BLINKA_FT232H=1 uv run python failsafe.py

# Using traditional python
BLINKA_FT232H=1 python failsafe.py
```

### Development Configuration

All configuration is handled through the `.env` file. See `.env.sample` for complete documentation including:

* **Core settings** - GPIO pin assignment and backup input configuration
* **Notification services** - Discord, GroupMe, and email setup
* **Optional features** - Timezone, Spinitron API, RabbitMQ integration
* **Advanced RabbitMQ** - Override commands and health check messaging

Copy `.env.sample` to `.env` (shortcut: `make env-copy`) and customize for your setup.

#### RabbitMQ Override Commands

To temporarily disable failsafe processing, you can send override messages to the RabbitMQ queue:

```bash
# Enable 5-minute override
curl -u username:password -H "Content-Type: application/json" \
     -X POST http://rabbitmq.example.com:15672/api/exchanges/%2F/wbor_failsafe_events/publish \
     -d '{
       "properties": {},
       "routing_key": "wbor_failsafe_override",
       "payload": "{\"action\": \"enable_override\", \"duration_minutes\": 5}",
       "payload_encoding": "string"
     }'

# Disable override (reverts to normal operation)
curl -u username:password -H "Content-Type: application/json" \
     -X POST http://rabbitmq.example.com:15672/api/exchanges/%2F/wbor_failsafe_events/publish \
     -d '{
       "properties": {},
       "routing_key": "wbor_failsafe_override",
       "payload": "{\"action\": \"disable_override\"}",
       "payload_encoding": "string"
     }'
```

Replace `username:password` and `rabbitmq.example.com` with your RabbitMQ credentials and hostname.

### Project Structure

```txt
├── .github/
│   └── workflows/
│       └── ci.yml            # CI/CD pipeline
├── failsafe.py               # Main application
├── utils/
│   ├── logging.py            # Logging configuration
│   ├── rabbitmq_publisher.py # RabbitMQ publishing
│   └── rabbitmq_consumer.py  # RabbitMQ consuming
├── pyproject.toml            # Project configuration
├── Makefile                  # Development commands
├── uv.lock                   # Dependency lock file
└── requirements.txt          # Python dependencies (legacy)
```

### Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature-name`
3. Make your changes and ensure they pass linting: `make check`
4. Commit your changes: `git commit -am 'Add feature'`
5. Push to the branch: `git push origin feature-name`, following a conventional commit style (e.g., `feat: add new feature xyz`)
6. Submit a pull request

## TODO

Pull requests are welcome! Here are some ideas for future improvements:

* [ ] Allow for multiple pins to be monitored (e.g., for multiple Failsafe Gadgets).
* [ ] Add support for other notification services (e.g., Slack, SMS).

## References

* [Angry Audio Failsafe Gadget](https://angryaudio.com/failsafegadget/)
* [Failsafe Gadget Manual](https://angryaudio.com/wp-content/uploads/2022/08/AA_FailsafeGadgetUserGuide_2208031.pdf)
* [Discord Webhooks](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks)
* [Discord Webhook Embed Object](https://discord.com/developers/docs/resources/message#embed-object)
* [GroupMe Bots](https://dev.groupme.com/bots/new)
* [CircuitPython Libraries on any Computer with FT232H](https://learn.adafruit.com/circuitpython-on-any-computer-with-ft232h/)
* [Pika RabbitMQ Client Library](https://pika.readthedocs.io/)
