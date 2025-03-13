# ElToque Exchange Rates

Get real-time exchange rates from ElToque for Cuban currencies.
![Kooha-2025-03-06-11-06-06-ezgif com-video-to-gif-converter](https://github.com/user-attachments/assets/2eff709f-c23d-4655-8f7e-08aab528981e)


## Description

`ulauncher-elToque` is an extension for [Ulauncher](https://ulauncher.io/) that provides real-time exchange rates for Cuban currencies using data from [ElToque](https://eltoque.com/).

## Features

- Fetches real-time exchange rates for Cuban currencies from ElToque.
- Provides international exchange rates via Yahoo Finance.
- Compares ElToque rates with international market rates.
- Supports currency conversion for both Cuban and international currencies.
- Generates trend charts for historical rate analysis.
- Stores historical data locally for offline access.
- Lightweight and easy to use.
- Seamless integration with Ulauncher.

## Installation

### Method 1: Via Ulauncher Extension Manager
1. Open Ulauncher preferences
2. Go to the "Extensions" tab
3. Click "Add Extension"
4. Paste the following URL: `https://github.com/hackroot9623/ulauncher-elToque`

### Method 2: Manual Installation
1. Clone the repository:
   ```bash
   git clone https://github.com/hackroot9623/ulauncher-elToque.git ~/.local/share/ulauncher/extensions/ulauncher-eltoque
   ```

2. Install the required dependencies:
   ```bash
   pip install -r ~/.local/share/ulauncher/extensions/ulauncher-eltoque/requirements.txt
   ```

3. Restart Ulauncher

### Troubleshooting
If you encounter errors during installation:
1. Make sure you have the required dependencies installed:
   ```bash
   pip install matplotlib numpy requests pillow
   ```
2. Check if your Python environment has access to install packages
3. Try installing the extension manually using Method 2 above

## Usage

1. Open Ulauncher.
2. Type the keyword you have set for this extension (default is `currency`).
3. Choose from three main options:
   - **ElToque Rates**: View Cuban exchange rates from ElToque
   - **International Rates**: View international exchange rates via Yahoo Finance
   - **Compare Rates**: Compare ElToque rates with international markets

### ElToque Rates
- View all current rates: Just type the keyword
- Convert currencies: `100 USD to EUR`
- Historical rates: `2024-03-01 100 USD to EUR` or `history 2024-03-01`
- Trend analysis: `USD trend 7d` (supports 7d, 30d, 3m, 6m, 1y)

### International Rates
- View major currencies: `international`
- Convert currencies: `international 100 USD to EUR`
- Trend analysis: `international EUR trend 30d`

### Compare Rates
- Compare all currencies: `compare`
- Compare specific currency: `compare EUR`

## Get Help
![image](https://github.com/user-attachments/assets/e2a45534-cb83-4cdd-bfa6-67151083da3c)


## Configuration

You can configure the keyword for the extension in the Ulauncher preferences window.

## Contributing

Contributions are welcome! Please fork the repository and submit a pull request.

## Acknowledgements

- [ElToque](https://eltoque.com/) for providing the exchange rate data.
- [Ulauncher](https://ulauncher.io/) for the awesome launcher platform.

## Contact

For any questions or suggestions, feel free to open an issue or contact the repository owner.
