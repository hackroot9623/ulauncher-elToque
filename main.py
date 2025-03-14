import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

# Try to import optional dependencies with helpful error messages
try:
    import requests
except ImportError:
    print("Error: 'requests' package is missing. Please install it with: pip install requests")
    raise

try:
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
except ImportError:
    print("Error: 'matplotlib' package is missing. Please install it with: pip install matplotlib")
    raise

try:
    import numpy as np
except ImportError:
    print("Error: 'numpy' package is missing. Please install it with: pip install numpy")
    raise

try:
    from PIL import Image
except ImportError:
    print("Error: 'pillow' package is missing. Please install it with: pip install pillow")
    raise

# Import Ulauncher modules
from ulauncher.api.client.Extension import Extension
from ulauncher.api.client.EventListener import EventListener
from ulauncher.api.shared.event import KeywordQueryEvent, PreferencesUpdateEvent, PreferencesEvent
from ulauncher.api.shared.item.ExtensionResultItem import ExtensionResultItem
from ulauncher.api.shared.action.RenderResultListAction import RenderResultListAction
from ulauncher.api.shared.action.CopyToClipboardAction import CopyToClipboardAction
from ulauncher.api.shared.action.OpenAction import OpenAction
from ulauncher.api.shared.action.SetUserQueryAction import SetUserQueryAction

# Global variables for caching
CACHE_DURATION = 300  # Cache duration in seconds (5 minutes)
last_api_call_time = None
cached_data = None
cached_date = None  # Store the date for which data is cached
trend_cache = {}  # Cache for trend data {currency_period: {dates: [], rates: []}}

# Default database path
DEFAULT_DB_PATH = os.path.expanduser("~/.local/share/ulauncher/eltoque_rates.db")
# Will be set properly when preferences are loaded
DB_PATH = DEFAULT_DB_PATH

def check_dependencies():
    """Check if all required dependencies are installed"""
    missing_deps = []
    
    try:
        import requests
    except ImportError:
        missing_deps.append("requests")
    
    try:
        import matplotlib
    except ImportError:
        missing_deps.append("matplotlib")
    
    try:
        import numpy
    except ImportError:
        missing_deps.append("numpy")
    
    try:
        from PIL import Image
    except ImportError:
        missing_deps.append("pillow")
    
    return missing_deps

class ElToqueExtension(Extension):
    def __init__(self):
        super(ElToqueExtension, self).__init__()
        self.subscribe(KeywordQueryEvent, KeywordQueryEventListener())
        self.subscribe(PreferencesEvent, PreferencesEventListener())
        self.subscribe(PreferencesUpdateEvent, PreferencesUpdateEventListener())
        
        # Initialize dependency_error attribute
        self.dependency_error = False
        
        # Ensure currency icons are available
        self.ensure_currency_icons()
        
        # Check for dependencies (if any)
        self.check_dependencies()
        
        # Default values
        self.api_key = None
        
        # Default currency icons mapping
        self.currency_icons = {
            "USD": "images/usd.png",
            "ECU": "images/eur.png",
            "MLC": "images/mlc.png",
            "TRX": "images/transfer.png",
            "USDT_TRC20": "images/usdt.png"
        }
        
        # Default currency display names mapping
        self.currency_names = {
            "USD": "USD",
            "ECU": "EUR",
            "MLC": "MLC",
            "TRX": "TRANSFER",
            "USDT_TRC20": "USDT"
        }
        
        # Default currency aliases for user input (maps what user types to API currency code)
        self.currency_aliases = {
            "USD": "USD",
            "EUR": "ECU",  # User types EUR, we look for ECU in API response
            "MLC": "MLC",
            "TRANSFER": "TRX",
            "USDT": "USDT_TRC20"
        }
        
        # Initialize the database
        self.init_database()

    def check_dependencies(self):
        """Check for required dependencies and set dependency_error if any are missing"""
        try:
            import requests
            import matplotlib
            # Add other dependencies as needed
        except ImportError as e:
            self.dependency_error = True
            print(f"Dependency error: {str(e)}")

    def init_database(self):
        """Initialize the SQLite database for storing historical rates"""
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        
        # Connect to the database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Create tables if they don't exist
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS rates (
            date TEXT,
            currency TEXT,
            rate REAL,
            PRIMARY KEY (date, currency)
        )
        ''')
        
        # Create index for faster queries
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_date ON rates (date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_currency ON rates (currency)')
        
        # Create metadata table for tracking last update
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        ''')
        
        # Commit changes and close connection
        conn.commit()
        conn.close()

    def ensure_currency_icons(self):
        """Ensure all currency icons are available, downloading missing ones"""
        # Currency to country code mapping
        currency_map = {
            "EUR": "eu",
            "GBP": "gb",
            "JPY": "jp",
            "CAD": "ca",
            "AUD": "au",
            "CHF": "ch",
            "CNY": "cn",
            "HKD": "hk"
        }
        
        for currency, country in currency_map.items():
            icon_path = f"images/{currency.lower()}.png"
            
            # Skip if icon already exists
            if os.path.exists(icon_path):
                continue
            
            # Download the flag
            try:
                url = f"https://flagcdn.com/w80/{country}.png"
                response = requests.get(url, timeout=5)
                response.raise_for_status()
                
                # Save the icon
                with open(icon_path, "wb") as f:
                    f.write(response.content)
                print(f"Downloaded icon for {currency}")
            except Exception as e:
                print(f"Failed to download icon for {currency}: {str(e)}")

class PreferencesEventListener(EventListener):
    def on_event(self, event, extension):
        global DB_PATH
        
        # Load preferences when the extension starts
        extension.api_key = event.preferences.get('api_key', '')
        
        # Set the database path if provided
        custom_db_path = event.preferences.get('db_path', '')
        if custom_db_path:
            # Expand user directory if path starts with ~
            if custom_db_path.startswith('~'):
                custom_db_path = os.path.expanduser(custom_db_path)
            DB_PATH = custom_db_path
        else:
            DB_PATH = DEFAULT_DB_PATH
        
        # Ensure the database directory exists
        db_dir = os.path.dirname(DB_PATH)
        if not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        
        # Initialize the database
        self.init_database()
        
        # Load custom icons if provided
        for currency in extension.currency_icons.keys():
            pref_key = f"{currency.lower()}_icon"
            custom_icon = event.preferences.get(pref_key, '')
            if custom_icon:
                extension.currency_icons[currency] = custom_icon
        
        # Load currency display names if provided
        for currency in extension.currency_names.keys():
            pref_key = f"{currency.lower()}_display"
            display_name = event.preferences.get(pref_key, '')
            if display_name:
                extension.currency_names[currency] = display_name
                
        # Set up the reverse mapping for aliases
        extension.currency_aliases = {}
        for api_currency in extension.currency_names.keys():
            display_name = extension.currency_names[api_currency]
            extension.currency_aliases[display_name] = api_currency

    def init_database(self):
        """Initialize the database if it doesn't exist"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Create tables if they don't exist
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS rates (
                date TEXT,
                currency TEXT,
                rate REAL,
                PRIMARY KEY (date, currency)
            )
            ''')
            
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            ''')
            
            # Commit changes and close connection
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Error initializing database: {str(e)}")

class PreferencesUpdateEventListener(EventListener):
    def on_event(self, event, extension):
        global DB_PATH
        
        # Update the API key if it changed
        if event.id == 'api_key':
            extension.api_key = event.new_value
        
        # Update the database path if it changed
        elif event.id == 'db_path':
            old_db_path = DB_PATH
            
            if event.new_value:
                # Expand user directory if path starts with ~
                if event.new_value.startswith('~'):
                    DB_PATH = os.path.expanduser(event.new_value)
                else:
                    DB_PATH = event.new_value
            else:
                DB_PATH = DEFAULT_DB_PATH
            
            # Ensure the database directory exists
            db_dir = os.path.dirname(DB_PATH)
            if not os.path.exists(db_dir):
                os.makedirs(db_dir, exist_ok=True)
            
            # If the path changed, migrate data from old to new
            if old_db_path != DB_PATH and os.path.exists(old_db_path):
                self.migrate_database(old_db_path, DB_PATH)
            else:
                # Initialize the new database
                self.init_database()
        
        # Update currency icons if they changed
        for currency in extension.currency_icons.keys():
            pref_key = f"{currency.lower()}_icon"
            if event.id == pref_key:
                extension.currency_icons[currency] = event.new_value
        
        # Update currency display names if they changed
        for currency in extension.currency_names.keys():
            pref_key = f"{currency.lower()}_display"
            if event.id == pref_key:
                extension.currency_names[currency] = event.new_value
                
        # Rebuild the aliases dictionary
        extension.currency_aliases = {}
        for api_currency in extension.currency_names.keys():
            display_name = extension.currency_names[api_currency]
            extension.currency_aliases[display_name] = api_currency

    def migrate_database(self, old_path, new_path):
        """Migrate data from old database to new database"""
        try:
            # Initialize the new database
            self.init_database()
            
            # Connect to both databases
            old_conn = sqlite3.connect(old_path)
            old_cursor = old_conn.cursor()
            
            new_conn = sqlite3.connect(new_path)
            new_cursor = new_conn.cursor()
            
            # Copy rates data
            old_cursor.execute("SELECT date, currency, rate FROM rates")
            rates_data = old_cursor.fetchall()
            
            if rates_data:
                new_cursor.executemany(
                    "INSERT OR REPLACE INTO rates (date, currency, rate) VALUES (?, ?, ?)",
                    rates_data
                )
            
            # Copy metadata
            old_cursor.execute("SELECT key, value FROM metadata")
            metadata = old_cursor.fetchall()
            
            if metadata:
                new_cursor.executemany(
                    "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                    metadata
                )
            
            # Commit changes and close connections
            new_conn.commit()
            old_conn.close()
            new_conn.close()
            
            print(f"Database migrated from {old_path} to {new_path}")
        except Exception as e:
            print(f"Error migrating database: {str(e)}")

class KeywordQueryEventListener(EventListener):
    def on_event(self, event, extension):
        # Check for dependency errors
        if extension.dependency_error:
            items = []
            items.append(ExtensionResultItem(
                icon='images/icon.png',
                name="Missing Dependencies",
                description="Please install the required dependencies.",
                on_enter=CopyToClipboardAction("pip install requests matplotlib")
            ))
            return RenderResultListAction(items)
        
        query = event.get_argument() or ""
        items = []

        # Check if API key is configured
        if not extension.api_key:
            items.append(ExtensionResultItem(
                icon='images/icon.png',
                name="API Key Missing",
                description="Please configure your API key in the extension settings.",
                on_enter=CopyToClipboardAction("API Key Missing")
            ))
            return RenderResultListAction(items)
        
        # Check if the query is for help
        if query.lower() == "help" or query.lower() == "?":
            return self.show_help(extension)
        
        # If no query, show the three main options
        if not query:
            # Option 1: ElToque Rates
            items.append(ExtensionResultItem(
                icon='images/icon.png',
                name="ElToque Rates",
                description="View Cuban exchange rates from ElToque",
                on_enter=SetUserQueryAction(f"{event.get_keyword()} eltoque")
            ))
            
            # Option 2: International Rates
            items.append(ExtensionResultItem(
                icon='images/globe.png',  # You'll need to add this icon
                name="International Rates",
                description="View international exchange rates via Yahoo Finance",
                on_enter=SetUserQueryAction(f"{event.get_keyword()} international")
            ))
            
            # Option 3: Compare Rates
            items.append(ExtensionResultItem(
                icon='images/compare.png',  # You'll need to add this icon
                name="Compare Rates",
                description="Compare ElToque rates with international markets",
                on_enter=SetUserQueryAction(f"{event.get_keyword()} compare")
            ))
            
            return RenderResultListAction(items)
        
        # Handle specific commands based on the first word
        command = query.lower().split()[0] if query else ""
        
        if command == "eltoque":
            # Handle ElToque rates (original functionality)
            return self.handle_eltoque_rates(query[8:].strip(), extension)
        elif command == "international":
            # Handle international rates
            return self.handle_international_rates(query[14:].strip(), extension)
        elif command == "compare":
            # Handle rate comparison
            return self.handle_rate_comparison(query[8:].strip(), extension)
        else:
            # Default to ElToque rates for backward compatibility
            return self.handle_eltoque_rates(query, extension)

    def handle_eltoque_rates(self, query, extension):
        """Handle ElToque exchange rates (original functionality)"""
        items = []
        
        # Check if the query is for database management
        if query.lower().startswith("db "):
            return self.handle_db_commands(query, extension)
        
        # Check if the query is for database history lookup
        if query.lower().startswith("history "):
            return self.handle_history_query(query, extension)
        
        # Check if the query is for a trend (e.g., "USD trend 7d")
        if "trend" in query.lower():
            try:
                parts = query.lower().split()
                if len(parts) < 3:
                    items.append(ExtensionResultItem(
                        icon='images/icon.png',
                        name="Invalid Trend Query",
                        description="Please use the format: 'USD trend 7d' (supports 7d, 30d, 3m, 6m, 1y)",
                        on_enter=CopyToClipboardAction("Invalid Trend Query")
                    ))
                else:
                    currency_input = parts[0].upper()
                    period = parts[2].lower()
                    
                    # Convert user input currency to API currency
                    currency = extension.currency_aliases.get(currency_input, currency_input)
                    
                    # Validate the period
                    valid_periods = {"7d": 7, "30d": 30, "3m": 90, "6m": 180, "1y": 365}
                    if period not in valid_periods:
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="Invalid Period",
                            description="Supported periods: 7d, 30d, 3m, 6m, 1y",
                            on_enter=CopyToClipboardAction("Invalid Period")
                        ))
                    else:
                        # Get trend data
                        days = valid_periods[period]
                        trend_data = self.get_trend_data(extension, currency, days)
                        
                        if not trend_data or len(trend_data["dates"]) == 0:
                            items.append(ExtensionResultItem(
                                icon='images/icon.png',
                                name="No Trend Data Available",
                                description=f"Could not retrieve trend data for {currency_input} over {period}",
                                on_enter=CopyToClipboardAction("No Trend Data Available")
                            ))
                        else:
                            dates = trend_data["dates"]
                            rates = trend_data["rates"]
                            
                            # Calculate statistics
                            min_rate = min(rates)
                            max_rate = max(rates)
                            avg_rate = sum(rates) / len(rates)
                            
                            # Calculate change
                            first_rate = rates[0]
                            last_rate = rates[-1]
                            change = last_rate - first_rate
                            change_pct = (change / first_rate) * 100 if first_rate != 0 else 0
                            
                            # Determine trend direction and icon
                            if change > 0:
                                trend_icon = "images/up.png"  # You'll need to add this icon
                                trend_symbol = "↑"
                            elif change < 0:
                                trend_icon = "images/down.png"  # You'll need to add this icon
                                trend_symbol = "↓"
                            else:
                                trend_icon = "images/flat.png"  # You'll need to add this icon
                                trend_symbol = "→"
                            
                            # Add header item with trend arrow
                            display_currency = extension.currency_names.get(currency, currency)
                            items.append(ExtensionResultItem(
                                icon=trend_icon,
                                name=f"{display_currency} Trend ({period}) {trend_symbol}",
                                description=f"Change: {change:.2f} ({change_pct:.2f}%)",
                                on_enter=CopyToClipboardAction(f"{display_currency} Trend ({period}): Change: {change:.2f} ({change_pct:.2f}%)")
                            ))
                            
                            # Add statistics items
                            items.append(ExtensionResultItem(
                                icon=extension.currency_icons.get(currency, "images/icon.png"),
                                name=f"Statistics for {period}",
                                description=f"Min: {min_rate:.2f} | Max: {max_rate:.2f} | Avg: {avg_rate:.2f}",
                                on_enter=CopyToClipboardAction(f"Min: {min_rate:.2f} | Max: {max_rate:.2f} | Avg: {avg_rate:.2f}")
                            ))
                            
                            # Add data points item
                            items.append(ExtensionResultItem(
                                icon=extension.currency_icons.get(currency, "images/icon.png"),
                                name=f"Data Points: {len(trend_data['dates'])}",
                                description=f"From {dates[0]} to {dates[-1]}",
                                on_enter=CopyToClipboardAction(f"Data Points: {len(trend_data['dates'])} from {dates[0]} to {dates[-1]}")
                            ))
                            
                            # Add option to generate chart
                            items.append(ExtensionResultItem(
                                icon="images/chart.png",
                                name="Generate Chart",
                                description=f"Click to generate and open a chart for {display_currency} trend",
                                on_enter=OpenAction(self.generate_trend_chart(dates, rates, currency, period))
                            ))
            except Exception as e:
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name="Error",
                    description=str(e),
                    on_enter=CopyToClipboardAction(str(e))
                ))
            
            return RenderResultListAction(items)
        else:
            # Parse the query to check for date format
            target_date = datetime.now().strftime("%Y-%m-%d")  # Default to today
            query_parts = query.lower().split()
            
            # Check if query contains a date (format: YYYY-MM-DD)
            date_index = -1
            for i, part in enumerate(query_parts):
                if self.is_date_format(part):
                    target_date = part
                    date_index = i
                    break
            
            # Remove the date from the query if found
            if date_index >= 0:
                query_parts.pop(date_index)
                query = " ".join(query_parts)
            
            # Check if the query is a calculation (e.g., "100 USD to EUR")
            if "to" in query.lower():
                try:
                    # Parse the input (e.g., "100 USD to EUR")
                    parts = query.lower().split()
                    amount = float(parts[0])  # Extract the amount
                    from_currency_input = parts[1].upper()  # Extract the source currency as input by user
                    to_currency_input = parts[3].upper()  # Extract the target currency as input by user
                    
                    # Convert user input currencies to API currencies
                    from_currency = extension.currency_aliases.get(from_currency_input, from_currency_input)
                    to_currency = extension.currency_aliases.get(to_currency_input, to_currency_input)

                    # Fetch exchange rates (with local storage)
                    data = self.fetch_exchange_rates(extension, target_date)

                    # Extract exchange rates
                    tasas = data.get("tasas", {})
                    if not tasas:
                        items = []
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="No data available",
                            description=f"No exchange rates found for {target_date}.",
                            on_enter=CopyToClipboardAction("No data available")
                        ))
                    else:
                        # Check if currencies are supported (CUP is always valid)
                        valid_from = from_currency == "CUP" 
                        if not valid_from:
                            valid_from = from_currency in tasas

                        valid_to = to_currency == "CUP"
                        if not valid_to:
                            valid_to = to_currency in tasas

                        if not valid_from or not valid_to:
                            items = []
                            items.append(ExtensionResultItem(
                                icon='images/icon.png',
                                name="Invalid Currency",
                                description=f"One or both currencies are not supported.",
                                on_enter=CopyToClipboardAction("Invalid Currency")
                            ))
                        else:
                            # Get the rates (CUP rate is 1:1)
                            from_rate = tasas[from_currency] if from_currency != "CUP" else 1
                            to_rate = tasas[to_currency] if to_currency != "CUP" else 1

                            # Calculate the conversion
                            result = (amount * from_rate) / to_rate

                            # Find appropriate display names for the result
                            from_display = from_currency_input
                            to_display = to_currency_input
                            
                            # Get the appropriate icon
                            from_icon = extension.currency_icons.get(from_currency, "images/icon.png")

                            # Display the result
                            date_info = f" ({target_date})" if target_date != datetime.now().strftime("%Y-%m-%d") else ""
                            items.append(ExtensionResultItem(
                                icon=from_icon,
                                name=f"{amount} {from_display} = {result:.2f} {to_display}{date_info}",
                                description=f"Exchange rate: 1 {from_display} = {from_rate / to_rate:.2f} {to_display}",
                                on_enter=CopyToClipboardAction(str(result))
                            ))

                except (IndexError, ValueError):
                    items = []
                    items.append(ExtensionResultItem(
                        icon='images/icon.png',
                        name="Invalid Input",
                        description="Please use the format: '100 USD to EUR' or 'YYYY-MM-DD 100 USD to EUR'",
                        on_enter=CopyToClipboardAction("Invalid Input")
                    ))
                except requests.exceptions.HTTPError as e:
                    if e.response.status_code == 429:
                        items = []
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="Rate Limit Exceeded",
                            description="Please wait a few minutes before trying again.",
                            on_enter=CopyToClipboardAction("Rate Limit Exceeded")
                        ))
                    elif e.response.status_code == 401:
                        items = []
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="Invalid API Key",
                            description="Please check your API key in the extension settings.",
                            on_enter=CopyToClipboardAction("Invalid API Key")
                        ))
                    else:
                        items = []
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="API Error",
                            description=f"HTTP Error: {str(e)}",
                            on_enter=CopyToClipboardAction(str(e))
                        ))
                except Exception as e:
                    items = []
                    items.append(ExtensionResultItem(
                        icon='images/icon.png',
                        name="Error",
                        description=str(e),
                        on_enter=CopyToClipboardAction(str(e))
                    ))
            else:
                # Default behavior: Show all exchange rates
                try:
                    # Fetch exchange rates (with local storage)
                    data = self.fetch_exchange_rates(extension, target_date)

                    # Extract exchange rates from the response
                    tasas = data.get("tasas", {})
                    if not tasas:
                        items = []
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="No data available",
                            description=f"No exchange rates found for {target_date}.",
                            on_enter=CopyToClipboardAction("No data available")
                        ))
                    else:
                        # Add a header item showing the date
                        if target_date != datetime.now().strftime("%Y-%m-%d"):
                            items = []
                            items.append(ExtensionResultItem(
                                icon='images/icon.png',
                                name=f"Exchange Rates for {target_date}",
                                description="Historical exchange rates",
                                on_enter=CopyToClipboardAction(target_date)
                            ))
                        
                        # Display each exchange rate
                        for currency, rate in tasas.items():
                            icon = extension.currency_icons.get(currency, "images/icon.png")
                            display_name = extension.currency_names.get(currency, currency)
                            items.append(ExtensionResultItem(
                                icon=icon,
                                name=f"{display_name}: {rate} CUP",
                                description=f"Exchange rate for {display_name}",
                                on_enter=CopyToClipboardAction(str(rate))
                            ))

                except requests.exceptions.HTTPError as e:
                    if e.response.status_code == 429:
                        items = []
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="Rate Limit Exceeded",
                            description="Please wait a few minutes before trying again.",
                            on_enter=CopyToClipboardAction("Rate Limit Exceeded")
                        ))
                    elif e.response.status_code == 401:
                        items = []
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="Invalid API Key",
                            description="Please check your API key in the extension settings.",
                            on_enter=CopyToClipboardAction("Invalid API Key")
                        ))
                    else:
                        items = []
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="API Error",
                            description=f"HTTP Error: {str(e)}",
                            on_enter=CopyToClipboardAction(str(e))
                        ))
                except requests.exceptions.RequestException as e:
                    # Try to get data from local storage if network error
                    offline_data = self.get_rates_from_db(target_date)
                    if offline_data:
                        items = []
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name=f"Offline Mode - {target_date}",
                            description="Using locally stored data (network unavailable)",
                            on_enter=CopyToClipboardAction("Offline Mode")
                        ))
                        
                        # Display each exchange rate from local storage
                        for currency, rate in offline_data.items():
                            icon = extension.currency_icons.get(currency, "images/icon.png")
                            display_name = extension.currency_names.get(currency, currency)
                            items.append(ExtensionResultItem(
                                icon=icon,
                                name=f"{display_name}: {rate} CUP",
                                description=f"Exchange rate for {display_name} (offline data)",
                                on_enter=CopyToClipboardAction(str(rate))
                            ))
                    else:
                        items = []
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="Network Error",
                            description=f"Failed to fetch data: {str(e)}",
                            on_enter=CopyToClipboardAction(str(e))
                        ))
                except json.JSONDecodeError as e:
                    items = []
                    items.append(ExtensionResultItem(
                        icon='images/icon.png',
                        name="JSON Error",
                        description=f"Invalid API response: {str(e)}",
                        on_enter=CopyToClipboardAction(str(e))
                    ))
                except Exception as e:
                    items = []
                    items.append(ExtensionResultItem(
                        icon='images/icon.png',
                        name="Error",
                        description=str(e),
                        on_enter=CopyToClipboardAction(str(e))
                    ))

        return RenderResultListAction(items)
    
    def handle_db_commands(self, query, extension):
        """Handle database management commands"""
        items = []
        parts = query.split()
        command = parts[1] if len(parts) > 1 else "help"
        
        if command == "status":
            # Get database status
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                
                # Get total number of records
                cursor.execute("SELECT COUNT(*) FROM rates")
                total_records = cursor.fetchone()[0]
                
                # Get date range
                cursor.execute("SELECT MIN(date), MAX(date) FROM rates")
                date_range = cursor.fetchone()
                min_date, max_date = date_range if date_range else ("N/A", "N/A")
                
                # Get currencies
                cursor.execute("SELECT DISTINCT currency FROM rates")
                currencies = [row[0] for row in cursor.fetchall()]
                
                # Get last update time
                cursor.execute("SELECT value FROM metadata WHERE key='last_update'")
                last_update = cursor.fetchone()
                last_update = last_update[0] if last_update else "Never"
                
                conn.close()
                
                # Display database status
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name="Database Status",
                    description=f"Total records: {total_records} | Date range: {min_date} to {max_date}",
                    on_enter=CopyToClipboardAction("Database Status")
                ))
                
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name="Currencies",
                    description=f"Stored currencies: {', '.join(currencies)}",
                    on_enter=CopyToClipboardAction(f"Stored currencies: {', '.join(currencies)}")
                ))
                
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name="Last Update",
                    description=f"Last database update: {last_update}",
                    on_enter=CopyToClipboardAction(f"Last database update: {last_update}")
                ))
                
            except Exception as e:
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name="Database Error",
                    description=f"Error accessing database: {str(e)}",
                    on_enter=CopyToClipboardAction(str(e))
                ))
                
        elif command == "clear":
            # Clear the database
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM rates")
                cursor.execute("DELETE FROM metadata")
                conn.commit()
                conn.close()
                
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name="Database Cleared",
                    description="All historical rate data has been deleted",
                    on_enter=CopyToClipboardAction("Database Cleared")
                ))
                
            except Exception as e:
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name="Database Error",
                    description=f"Error clearing database: {str(e)}",
                    on_enter=CopyToClipboardAction(str(e))
                ))
                
        elif command == "backup":
            # Backup the database
            try:
                backup_path = os.path.expanduser("~/eltoque_rates_backup.db")
                
                # Copy the database file
                import shutil
                shutil.copy2(DB_PATH, backup_path)
                
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name="Database Backup Created",
                    description=f"Backup saved to: {backup_path}",
                    on_enter=CopyToClipboardAction(f"Backup saved to: {backup_path}")
                ))
                
            except Exception as e:
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name="Backup Error",
                    description=f"Error creating backup: {str(e)}",
                    on_enter=CopyToClipboardAction(str(e))
                ))
                
        elif command == "restore":
            # Restore from backup
            try:
                backup_path = os.path.expanduser("~/eltoque_rates_backup.db")
                
                if not os.path.exists(backup_path):
                    items.append(ExtensionResultItem(
                        icon='images/icon.png',
                        name="Restore Error",
                        description="Backup file not found",
                        on_enter=CopyToClipboardAction("Backup file not found")
                    ))
                else:
                    # Copy the backup file to the database location
                    import shutil
                    shutil.copy2(backup_path, DB_PATH)
                    
                    items.append(ExtensionResultItem(
                        icon='images/icon.png',
                        name="Database Restored",
                        description="Database has been restored from backup",
                        on_enter=CopyToClipboardAction("Database Restored")
                    ))
                
            except Exception as e:
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name="Restore Error",
                    description=f"Error restoring database: {str(e)}",
                    on_enter=CopyToClipboardAction(str(e))
                ))
                
        elif command == "rebuild":
            # Rebuild the database (clear and fetch last 30 days)
            try:
                # Clear the database
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM rates")
                cursor.execute("DELETE FROM metadata")
                conn.commit()
                conn.close()
                
                # Fetch data for the last 30 days
                end_date = datetime.now()
                start_date = end_date - timedelta(days=30)
                
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name="Rebuilding Database",
                    description="Fetching data for the last 30 days...",
                    on_enter=CopyToClipboardAction("Rebuilding Database")
                ))
                
                # Start the rebuild process in the background
                self.rebuild_database(extension, start_date, end_date)
                
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name="Rebuild Initiated",
                    description="Database rebuild has been started in the background",
                    on_enter=CopyToClipboardAction("Database Rebuild Initiated")
                ))
                
            except Exception as e:
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name="Rebuild Error",
                    description=f"Error rebuilding database: {str(e)}",
                    on_enter=CopyToClipboardAction(str(e))
                ))
        else:
            # Help command
            items.append(ExtensionResultItem(
                icon='images/icon.png',
                name="Database Commands",
                description="Available commands: status, clear, backup, restore, rebuild",
                on_enter=CopyToClipboardAction("Database Commands")
            ))
            
            items.append(ExtensionResultItem(
                icon='images/icon.png',
                name="db status",
                description="Show database statistics and information",
                on_enter=CopyToClipboardAction("db status")
            ))
            
            items.append(ExtensionResultItem(
                icon='images/icon.png',
                name="db clear",
                description="Clear all stored historical rates",
                on_enter=CopyToClipboardAction("db clear")
            ))
            
            items.append(ExtensionResultItem(
                icon='images/icon.png',
                name="db backup",
                description="Create a backup of the database",
                on_enter=CopyToClipboardAction("db backup")
            ))
            
            items.append(ExtensionResultItem(
                icon='images/icon.png',
                name="db restore",
                description="Restore database from backup",
                on_enter=CopyToClipboardAction("db restore")
            ))
            
            items.append(ExtensionResultItem(
                icon='images/icon.png',
                name="db rebuild",
                description="Rebuild database with last 30 days of data",
                on_enter=CopyToClipboardAction("db rebuild")
            ))
            
        return RenderResultListAction(items)
    
    def rebuild_database(self, extension, start_date, end_date):
        """Rebuild the database with historical data in the background"""
        import threading
        
        def rebuild_task():
            current_date = start_date
            while current_date <= end_date:
                date_str = current_date.strftime("%Y-%m-%d")
                try:
                    # Fetch data from API
                    self.fetch_exchange_rates(extension, date_str, force_api=True)
                except Exception:
                    # Skip days with errors
                    pass
                
                # Move to next day
                current_date += timedelta(days=1)

    def fetch_exchange_rates(self, extension, target_date, force_api=False):
        """Fetch exchange rates from local storage or ElToque API with caching"""
        global last_api_call_time, cached_data, cached_date
        
        now = time.time()
        # Use memory cache if available and not expired and for the same date
        if (not force_api and cached_data and last_api_call_time and cached_date == target_date and 
            (now - last_api_call_time) < CACHE_DURATION):
            return cached_data
        
        # Check if we have data in the local database
        if not force_api:
            db_data = self.get_rates_from_db(target_date)
            if db_data:
                # Update memory cache
                cached_data = {"tasas": db_data}
                cached_date = target_date
                last_api_call_time = now
                return cached_data
        
        # Fetch new data from API
        date_from = f"{target_date} 00:00:01"
        date_to = f"{target_date} 23:59:01"
        url = f"https://tasas.eltoque.com/v1/trmi?date_from={date_from}&date_to={date_to}"
        headers = {
            "accept": "*/*",
            "Authorization": f"Bearer {extension.api_key}"
        }
        response = requests.get(url, headers=headers)
        response.raise_for_status()  # Raise an exception for HTTP errors
        data = response.json()
        
        # Update memory cache
        cached_data = data
        cached_date = target_date
        last_api_call_time = now
        
        # Store in local database
        self.store_rates_in_db(target_date, data.get("tasas", {}))
        
        return data

    def get_rates_from_db(self, date):
        """Retrieve exchange rates for a specific date from the local database"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Query the database for rates on the specified date
            cursor.execute("SELECT currency, rate FROM rates WHERE date = ?", (date,))
            results = cursor.fetchall()
            
            # Close the connection
            conn.close()
            
            # If we have results, format them as a dictionary
            if results:
                return {currency: rate for currency, rate in results}
            
            return None
        except Exception as e:
            print(f"Database error: {str(e)}")
            return None

    def store_rates_in_db(self, date, rates):
        """Store exchange rates in the local database"""
        if not rates:
            return
        
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Insert or update rates for each currency
            for currency, rate in rates.items():
                cursor.execute(
                    "INSERT OR REPLACE INTO rates (date, currency, rate) VALUES (?, ?, ?)",
                    (date, currency, rate)
                )
            
            # Update the last_update metadata
            cursor.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                ("last_update", datetime.now().isoformat())
            )
            
            # Commit and close
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Database error: {str(e)}")

    def get_trend_data(self, extension, currency, period_days):
        """Get trend data for a currency over a specified number of days"""
        global trend_cache
        
        # Check if we have cached data for this currency and period
        cache_key = f"{currency}_{period_days}"
        if cache_key in trend_cache and (time.time() - trend_cache[cache_key]["timestamp"]) < CACHE_DURATION:
            return trend_cache[cache_key]
        
        # Calculate date range
        end_date = datetime.now()
        start_date = end_date - timedelta(days=period_days)
        start_date_str = start_date.strftime("%Y-%m-%d")
        
        # Initialize data structures
        all_dates = []
        all_rates = {}  # Changed to dictionary: {currency: [rates]}
        missing_dates = []
        
        # Get all supported currencies
        supported_currencies = list(extension.currency_names.keys())
        
        # Initialize rates list for each currency
        for curr in supported_currencies:
            all_rates[curr] = []
        
        # First, get all dates in the range
        current_date = start_date
        while current_date <= end_date:
            all_dates.append(current_date.strftime("%Y-%m-%d"))
            current_date += timedelta(days=1)
        
        # Try to get data from the local database first
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Query the database for trend data for ALL currencies
            cursor.execute(
                "SELECT date, currency, rate FROM rates WHERE date >= ? AND date <= ? ORDER BY date",
                (start_date_str, end_date.strftime("%Y-%m-%d"))
            )
            db_results = cursor.fetchall()
            conn.close()
            
            # Create a dictionary of existing data: {date: {currency: rate}}
            db_data = {}
            for date, curr, rate in db_results:
                if date not in db_data:
                    db_data[date] = {}
                db_data[date][curr] = rate
            
            # Check which dates are missing data for any currency
            for date_str in all_dates:
                date_has_all_currencies = True
                
                # Initialize with None for all currencies on this date
                for curr in supported_currencies:
                    if date_str in db_data and curr in db_data[date_str]:
                        all_rates[curr].append(db_data[date_str][curr])
                    else:
                        all_rates[curr].append(None)
                        date_has_all_currencies = False
                
                # If any currency is missing for this date, add to missing dates
                if not date_has_all_currencies:
                    missing_dates.append(date_str)
                
        except Exception as e:
            print(f"Database error in get_trend_data: {str(e)}")
            # If database query fails, all dates are missing
            missing_dates = all_dates
            for curr in supported_currencies:
                all_rates[curr] = [None] * len(all_dates)
        
        # Fetch missing data from API
        if missing_dates:
            print(f"Fetching {len(missing_dates)} missing dates from API for all currencies")
            for date_str in missing_dates:
                try:
                    data = self.fetch_exchange_rates(extension, date_str, force_api=True)
                    tasas = data.get("tasas", {})
                    
                    # Update rates for all currencies on this date
                    if tasas:
                        idx = all_dates.index(date_str)
                        for curr in supported_currencies:
                            if curr in tasas:
                                all_rates[curr][idx] = tasas[curr]
                except Exception as e:
                    print(f"Error fetching data for {date_str}: {str(e)}")
                    # Keep the None values for this date
        
        # Process data for the requested currency
        # Remove any None values (dates with no data)
        valid_data = [(date, rate) for date, rate in zip(all_dates, all_rates[currency]) if rate is not None]
        
        if not valid_data:
            return {"dates": [], "rates": [], "timestamp": time.time()}
        
        # Unzip the valid data
        valid_dates, valid_rates = zip(*valid_data)
        
        # Cache the result for the requested currency
        result = {
            "dates": valid_dates,
            "rates": valid_rates,
            "timestamp": time.time()
        }
        trend_cache[cache_key] = result
        
        # Also cache results for other currencies while we're at it
        for curr in supported_currencies:
            if curr != currency:
                curr_valid_data = [(date, rate) for date, rate in zip(all_dates, all_rates[curr]) if rate is not None]
                if curr_valid_data:
                    curr_valid_dates, curr_valid_rates = zip(*curr_valid_data)
                    curr_result = {
                        "dates": curr_valid_dates,
                        "rates": curr_valid_rates,
                        "timestamp": time.time()
                    }
                    trend_cache[f"{curr}_{period_days}"] = curr_result
        
        return result

    def generate_trend_chart(self, dates, rates, currency, period):
        """Generate a chart for the trend data and save it to a temporary file"""
        # Create a temporary directory if it doesn't exist
        temp_dir = os.path.expanduser("~/.cache/ulauncher_eltoque")
        os.makedirs(temp_dir, exist_ok=True)
        
        # Create a unique filename
        filename = f"{temp_dir}/{currency}_{period}_{int(time.time())}.png"
        
        try:
            # Create the chart
            plt.figure(figsize=(10, 6))
            
            # Convert string dates to datetime objects for better handling
            datetime_dates = [datetime.strptime(date, "%Y-%m-%d") for date in dates]
            
            # Plot the data
            plt.plot(datetime_dates, rates, marker='o', linestyle='-', color='#1f77b4')
            
            # Set title and labels
            plt.title(f"{currency} to CUP Exchange Rate Trend ({period})")
            plt.xlabel("Date")
            plt.ylabel("Rate (CUP)")
            plt.grid(True, linestyle='--', alpha=0.7)
            
            # Configure x-axis date formatting based on the period
            ax = plt.gca()
            
            # Determine appropriate date format and tick frequency based on period
            if period == "7d":
                # For 7 days, show every day with day-month format
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%d-%b'))
                ax.xaxis.set_major_locator(mdates.DayLocator(interval=1))
            elif period == "30d":
                # For 30 days, show every 5 days
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%d-%b'))
                ax.xaxis.set_major_locator(mdates.DayLocator(interval=5))
            elif period == "3m":
                # For 3 months, show every 2 weeks
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%d-%b'))
                ax.xaxis.set_major_locator(mdates.DayLocator(interval=14))
            elif period == "6m":
                # For 6 months, show monthly
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%b-%Y'))
                ax.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
            elif period == "1y":
                # For 1 year, show every 2 months
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%b-%Y'))
                ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
            
            plt.xticks(rotation=45)
            
            # Add some visual improvements
            if len(dates) > 1:
                # Add trend line (using a polynomial fit for smoother line)
                if len(dates) > 5:
                    # For longer periods, add a trend line
                    z = np.polyfit(range(len(datetime_dates)), rates, 1)
                    p = np.poly1d(z)
                    plt.plot(datetime_dates, p(range(len(datetime_dates))), 'r--', alpha=0.5, 
                             label=f"Trend: {'+' if z[0] > 0 else ''}{z[0]:.4f} per day")
                    plt.legend()
                
                # Highlight min and max points
                min_rate = min(rates)
                max_rate = max(rates)
                min_idx = rates.index(min_rate)
                max_idx = rates.index(max_rate)
                
                plt.plot(datetime_dates[min_idx], min_rate, 'go', markersize=10)
                plt.plot(datetime_dates[max_idx], max_rate, 'ro', markersize=10)
                
                # Add annotations
                plt.annotate(f"Min: {min_rate:.2f}", 
                            (datetime_dates[min_idx], min_rate),
                            xytext=(10, -20),
                            textcoords="offset points",
                            arrowprops=dict(arrowstyle="->"))
                
                plt.annotate(f"Max: {max_rate:.2f}", 
                            (datetime_dates[max_idx], max_rate),
                            xytext=(10, 20),
                            textcoords="offset points",
                            arrowprops=dict(arrowstyle="->"))
            
            # Add currency icon to the top left corner
            try:
                from matplotlib.offsetbox import OffsetImage, AnnotationBbox
                from PIL import Image
                
                # Get the icon path for the currency
                icon_path = "images/icon.png"  # Default icon
                
                # Try to find the specific currency icon
                currency_lower = currency.lower()
                possible_paths = [
                    f"images/{currency_lower}.png",
                    f"images/{currency}.png"
                ]
                for path in possible_paths:
                    if os.path.exists(path):
                        icon_path = path
                        break
                
                # Check if the icon exists
                if os.path.exists(icon_path):
                    # Load the image
                    img = Image.open(icon_path)
                    
                    # Resize if needed (adjust size as needed)
                    img = img.resize((64, 64), Image.LANCZOS)
                    
                    # Convert to array for matplotlib
                    img_array = np.array(img)
                    
                    # Create an OffsetImage
                    imagebox = OffsetImage(img_array, zoom=0.5)
                    imagebox.image.axes = ax
                    
                    # Create an AnnotationBbox
                    ab = AnnotationBbox(
                        imagebox,
                        (0.05, 0.95),  # Position in axes coordinates (top left)
                        xycoords='axes fraction',
                        frameon=False  # No border around the image
                    )
                    
                    # Add the image to the plot
                    ax.add_artist(ab)
            except Exception as e:
                print(f"Warning: Could not add currency icon to chart: {str(e)}")
            
            plt.tight_layout()
            
            # Save the chart
            plt.savefig(filename, dpi=100)
            plt.close()
            
            return filename
        except Exception as e:
            print(f"Error generating trend chart: {str(e)}")
            # Return a placeholder or None if chart generation fails
            return None

    def handle_history_query(self, query, extension):
        """Handle history queries to check rates for specific dates"""
        items = []
        
        # Parse the query
        parts = query.split()
        
        # Check if we have enough parts (history DATE [CURRENCY])
        if len(parts) < 2:
            items.append(ExtensionResultItem(
                icon='images/icon.png',
                name="Invalid History Query",
                description="Usage: history YYYY-MM-DD [CURRENCY]",
                on_enter=CopyToClipboardAction("Invalid History Query")
            ))
            return RenderResultListAction(items)
        
        # Extract date and optional currency
        date_str = parts[1]
        currency = parts[2].upper() if len(parts) > 2 else None
        
        # Validate date format
        if not self.is_date_format(date_str):
            items.append(ExtensionResultItem(
                icon='images/icon.png',
                name="Invalid Date Format",
                description="Please use YYYY-MM-DD format",
                on_enter=CopyToClipboardAction("Invalid Date Format")
            ))
            return RenderResultListAction(items)
        
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            if currency:
                # If currency is specified, convert user input to API currency
                api_currency = extension.currency_aliases.get(currency, currency)
                
                # Query for specific currency on that date
                cursor.execute(
                    "SELECT rate FROM rates WHERE date = ? AND currency = ?", 
                    (date_str, api_currency)
                )
                result = cursor.fetchone()
                
                if result:
                    rate = result[0]
                    display_currency = extension.currency_names.get(api_currency, api_currency)
                    
                    items.append(ExtensionResultItem(
                        icon=extension.currency_icons.get(api_currency, "images/icon.png"),
                        name=f"{display_currency} Rate on {date_str}",
                        description=f"{display_currency}: {rate:.2f} CUP",
                        on_enter=CopyToClipboardAction(f"{display_currency}: {rate:.2f} CUP on {date_str}")
                    ))
                else:
                    # Try to fetch from API if not in database
                    try:
                        data = self.fetch_exchange_rates(extension, date_str, force_api=True)
                        tasas = data.get("tasas", {})
                        
                        if api_currency in tasas:
                            rate = tasas[api_currency]
                            display_currency = extension.currency_names.get(api_currency, api_currency)
                            
                            items.append(ExtensionResultItem(
                                icon=extension.currency_icons.get(api_currency, "images/icon.png"),
                                name=f"{display_currency} Rate on {date_str}",
                                description=f"{display_currency}: {rate:.2f} CUP (from API)",
                                on_enter=CopyToClipboardAction(f"{display_currency}: {rate:.2f} CUP on {date_str}")
                            ))
                        else:
                            items.append(ExtensionResultItem(
                                icon='images/icon.png',
                                name="Rate Not Found",
                                description=f"No rate found for {currency} on {date_str}",
                                on_enter=CopyToClipboardAction(f"No rate found for {currency} on {date_str}")
                            ))
                    except Exception as e:
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="API Error",
                            description=f"Could not fetch from API: {str(e)}",
                            on_enter=CopyToClipboardAction(str(e))
                        ))
            else:
                # Query for all currencies on that date
                cursor.execute(
                    "SELECT currency, rate FROM rates WHERE date = ? ORDER BY currency", 
                    (date_str,)
                )
                results = cursor.fetchall()
                
                if results:
                    # Add a header item
                    items.append(ExtensionResultItem(
                        icon='images/icon.png',
                        name=f"Exchange Rates for {date_str}",
                        description=f"Found {len(results)} currencies in database",
                        on_enter=CopyToClipboardAction(f"Exchange Rates for {date_str}")
                    ))
                    
                    # Add each currency rate
                    for api_currency, rate in results:
                        display_currency = extension.currency_names.get(api_currency, api_currency)
                        items.append(ExtensionResultItem(
                            icon=extension.currency_icons.get(api_currency, "images/icon.png"),
                            name=f"{display_currency}",
                            description=f"{rate:.2f} CUP",
                            on_enter=CopyToClipboardAction(f"{display_currency}: {rate:.2f} CUP on {date_str}")
                        ))
                else:
                    # Try to fetch from API if not in database
                    try:
                        data = self.fetch_exchange_rates(extension, date_str, force_api=True)
                        tasas = data.get("tasas", {})
                        
                        if tasas:
                            # Add a header item
                            items.append(ExtensionResultItem(
                                icon='images/icon.png',
                                name=f"Exchange Rates for {date_str}",
                                description=f"Found {len(tasas)} currencies from API",
                                on_enter=CopyToClipboardAction(f"Exchange Rates for {date_str}")
                            ))
                            
                            # Add each currency rate
                            for api_currency, rate in tasas.items():
                                display_currency = extension.currency_names.get(api_currency, api_currency)
                                items.append(ExtensionResultItem(
                                    icon=extension.currency_icons.get(api_currency, "images/icon.png"),
                                    name=f"{display_currency}",
                                    description=f"{rate:.2f} CUP (from API)",
                                    on_enter=CopyToClipboardAction(f"{display_currency}: {rate:.2f} CUP on {date_str}")
                                ))
                        else:
                            items.append(ExtensionResultItem(
                                icon='images/icon.png',
                                name="No Data Available",
                                description=f"No exchange rates found for {date_str}",
                                on_enter=CopyToClipboardAction(f"No exchange rates found for {date_str}")
                            ))
                    except Exception as e:
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="API Error",
                            description=f"Could not fetch from API: {str(e)}",
                            on_enter=CopyToClipboardAction(str(e))
                        ))
            
            conn.close()
            
        except Exception as e:
            items.append(ExtensionResultItem(
                icon='images/icon.png',
                name="Database Error",
                description=str(e),
                on_enter=CopyToClipboardAction(str(e))
            ))
        
        return RenderResultListAction(items)

    def is_date_format(self, text):
        """Check if the text is in YYYY-MM-DD format"""
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    def show_help(self, extension):
        """Show help information about all available commands"""
        items = []
        
        # Main features
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="ElToque Exchange Rates - Help",
            description="Overview of all available commands and features",
            on_enter=CopyToClipboardAction("ElToque Exchange Rates Help")
        ))
        
        # Main options
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="Main Options",
            description="ElToque Rates, International Rates, Compare",
            on_enter=CopyToClipboardAction("Main Options")
        ))
        
        # ElToque Rates
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="ElToque Rates",
            description="View Cuban exchange rates from ElToque",
            on_enter=CopyToClipboardAction("ElToque Rates")
        ))
        
        # International Rates
        items.append(ExtensionResultItem(
            icon='images/globe.png',
            name="International Rates",
            description="View international exchange rates",
            on_enter=CopyToClipboardAction("International Rates")
        ))
        
        # Compare Rates
        items.append(ExtensionResultItem(
            icon='images/compare.png',
            name="Compare Rates",
            description="Compare ElToque rates with international markets",
            on_enter=CopyToClipboardAction("Compare Rates")
        ))
        
        # Basic usage
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="Basic Usage",
            description="Type the keyword alone to see the main options",
            on_enter=CopyToClipboardAction("Basic Usage: Type the keyword alone to see the main options")
        ))
        
        # ElToque currency conversion
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="ElToque Currency Conversion",
            description="Example: 'eltoque 100 USD to EUR' or '100 USD to EUR'",
            on_enter=CopyToClipboardAction("ElToque Currency Conversion: 100 USD to EUR")
        ))
        
        # International currency conversion
        items.append(ExtensionResultItem(
            icon='images/globe.png',
            name="International Currency Conversion",
            description="Example: 'international 100 USD to EUR'",
            on_enter=CopyToClipboardAction("International Currency Conversion: international 100 USD to EUR")
        ))
        
        # Historical rates
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="Historical Rates",
            description="Example: 'eltoque 2024-03-01 100 USD to EUR' or 'history 2024-03-01'",
            on_enter=CopyToClipboardAction("Historical Rates: 2024-03-01 100 USD to EUR")
        ))
        
        # Trend analysis
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="Trend Analysis",
            description="Example: 'eltoque USD trend 7d' or 'international EUR trend 30d'",
            on_enter=CopyToClipboardAction("Trend Analysis: USD trend 7d")
        ))
        
        # Database commands
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="Database Management",
            description="Commands: 'db status', 'db clear', 'db backup', 'db restore', 'db rebuild'",
            on_enter=CopyToClipboardAction("Database Management: db status")
        ))
        
        # Compare specific currency
        items.append(ExtensionResultItem(
            icon='images/compare.png',
            name="Compare Specific Currency",
            description="Example: 'compare EUR' to compare only EUR rates",
            on_enter=CopyToClipboardAction("Compare Specific Currency: compare EUR")
        ))
        
        # Add database location
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="Database Location",
            description=f"Current database path: {DB_PATH}",
            on_enter=CopyToClipboardAction(f"Database path: {DB_PATH}")
        ))
        
        return RenderResultListAction(items)

    def handle_international_rates(self, query, extension):
        """Handle international exchange rates"""
        items = []
        
        try:
            # Check if the query is for a trend
            if "trend" in query.lower():
                return self.handle_international_trend(query, extension)
            
            # Parse the query to check for conversion
            if "to" in query.lower():
                return self.handle_international_conversion(query, extension)
            
            # Default: show major international currencies
            rates = self.fetch_international_rates()
            
            if not rates:
                items.append(ExtensionResultItem(
                    icon='images/globe.png',
                    name="No International Data Available",
                    description="Could not fetch international exchange rates.",
                    on_enter=CopyToClipboardAction("No International Data Available")
                ))
            else:
                # Add header
                items.append(ExtensionResultItem(
                    icon='images/globe.png',
                    name="International Exchange Rates",
                    description=f"Base currency: USD - {datetime.now().strftime('%Y-%m-%d')}",
                    on_enter=CopyToClipboardAction("International Exchange Rates")
                ))
                
                # Add major currencies
                major_currencies = ["EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY", "HKD"]
                for currency in major_currencies:
                    if currency in rates:
                        rate = rates[currency]
                        
                        # Check for currency icon
                        icon_path = f"images/{currency.lower()}.png"
                        if not os.path.exists(icon_path):
                            icon_path = "images/globe.png"  # Default icon
                        
                        items.append(ExtensionResultItem(
                            icon=icon_path,
                            name=f"{currency}: {rate:.4f}",
                            description=f"1 USD = {rate:.4f} {currency}",
                            on_enter=CopyToClipboardAction(str(rate))
                        ))
        except Exception as e:
            items.append(ExtensionResultItem(
                icon='images/globe.png',
                name="Error",
                description=str(e),
                on_enter=CopyToClipboardAction(str(e))
            ))
        
        return RenderResultListAction(items)

    def handle_international_conversion(self, query, extension):
        """Handle international currency conversion"""
        items = []
        
        try:
            # Parse the input (e.g., "100 USD to EUR")
            parts = query.lower().split()
            amount = float(parts[0])  # Extract the amount
            from_currency = parts[1].upper()  # Extract the source currency
            to_currency = parts[3].upper()  # Extract the target currency
            
            # Fetch exchange rates
            rates = self.fetch_international_rates()
            
            if not rates:
                items.append(ExtensionResultItem(
                    icon='images/globe.png',
                    name="No International Data Available",
                    description="Could not fetch international exchange rates.",
                    on_enter=CopyToClipboardAction("No International Data Available")
                ))
            else:
                # Calculate conversion
                result = self.convert_international_currency(amount, from_currency, to_currency, rates)
                
                # Display the result
                items.append(ExtensionResultItem(
                    icon='images/globe.png',
                    name=f"{amount} {from_currency} = {result:.2f} {to_currency}",
                    description=f"International market rate",
                    on_enter=CopyToClipboardAction(str(result))
                ))
        except (IndexError, ValueError):
            items.append(ExtensionResultItem(
                icon='images/globe.png',
                name="Invalid Input",
                description="Please use the format: '100 USD to EUR'",
                on_enter=CopyToClipboardAction("Invalid Input")
            ))
        except Exception as e:
            items.append(ExtensionResultItem(
                icon='images/globe.png',
                name="Error",
                description=str(e),
                on_enter=CopyToClipboardAction(str(e))
            ))
        
        return RenderResultListAction(items)

    def handle_international_trend(self, query, extension):
        """Handle international currency trend analysis"""
        items = []
        
        try:
            parts = query.lower().split()
            if len(parts) < 3:
                items.append(ExtensionResultItem(
                    icon='images/globe.png',
                    name="Invalid Trend Query",
                    description="Please use the format: 'EUR trend 7d' (supports 7d, 30d, 3m, 6m, 1y)",
                    on_enter=CopyToClipboardAction("Invalid Trend Query")
                ))
            else:
                currency = parts[0].upper()
                period = parts[2].lower()
                
                # Validate the period
                valid_periods = {"7d": 7, "30d": 30, "3m": 90, "6m": 180, "1y": 365}
                if period not in valid_periods:
                    items.append(ExtensionResultItem(
                        icon='images/globe.png',
                        name="Invalid Period",
                        description="Supported periods: 7d, 30d, 3m, 6m, 1y",
                        on_enter=CopyToClipboardAction("Invalid Period")
                    ))
                else:
                    # Get trend data
                    days = valid_periods[period]
                    trend_data = self.get_international_trend_data(currency, days)
                    
                    if not trend_data or len(trend_data["dates"]) == 0:
                        items.append(ExtensionResultItem(
                            icon='images/globe.png',
                            name="No Trend Data Available",
                            description=f"Could not retrieve trend data for {currency} over {period}",
                            on_enter=CopyToClipboardAction("No Trend Data Available")
                        ))
                    else:
                        # Process and display trend data (similar to the original trend code)
                        dates = trend_data["dates"]
                        rates = trend_data["rates"]
                        
                        # Calculate statistics
                        min_rate = min(rates)
                        max_rate = max(rates)
                        avg_rate = sum(rates) / len(rates)
                        
                        # Calculate change
                        first_rate = rates[0]
                        last_rate = rates[-1]
                        change = last_rate - first_rate
                        change_pct = (change / first_rate) * 100 if first_rate != 0 else 0
                        
                        # Determine trend direction and icon
                        if change > 0:
                            trend_icon = "images/up.png"
                            trend_symbol = "↑"
                        elif change < 0:
                            trend_icon = "images/down.png"
                            trend_symbol = "↓"
                        else:
                            trend_icon = "images/flat.png"
                            trend_symbol = "→"
                        
                        # Add header item with trend arrow
                        items.append(ExtensionResultItem(
                            icon=trend_icon,
                            name=f"{currency} Trend ({period}) {trend_symbol}",
                            description=f"Change: {change:.4f} ({change_pct:.2f}%)",
                            on_enter=CopyToClipboardAction(f"{currency} Trend ({period}): Change: {change:.4f} ({change_pct:.2f}%)")
                        ))
                        
                        # Add statistics items
                        items.append(ExtensionResultItem(
                            icon='images/globe.png',
                            name=f"Statistics for {period}",
                            description=f"Min: {min_rate:.4f} | Max: {max_rate:.4f} | Avg: {avg_rate:.4f}",
                            on_enter=CopyToClipboardAction(f"Min: {min_rate:.4f} | Max: {max_rate:.4f} | Avg: {avg_rate:.4f}")
                        ))
                        
                        # Add data points item
                        items.append(ExtensionResultItem(
                            icon='images/globe.png',
                            name=f"Data Points: {len(trend_data['dates'])}",
                            description=f"From {dates[0]} to {dates[-1]}",
                            on_enter=CopyToClipboardAction(f"Data Points: {len(trend_data['dates'])} from {dates[0]} to {dates[-1]}")
                        ))
                        
                        # Add option to generate chart
                        items.append(ExtensionResultItem(
                            icon="images/chart.png",
                            name="Generate Chart",
                            description=f"Click to generate and open a chart for {currency} trend",
                            on_enter=OpenAction(self.generate_international_trend_chart(dates, rates, currency, period))
                        ))
        except Exception as e:
            items.append(ExtensionResultItem(
                icon='images/globe.png',
                name="Error",
                description=str(e),
                on_enter=CopyToClipboardAction(str(e))
            ))
        
        return RenderResultListAction(items)

    def fetch_international_rates(self):
        """Fetch international exchange rates using a public API"""
        try:
            # Use a free exchange rate API (replace with your preferred API)
            url = "https://open.er-api.com/v6/latest/USD"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get("result") == "success":
                return data.get("rates", {})
            return None
        except Exception as e:
            print(f"Error fetching international rates: {str(e)}")
            return None

    def convert_international_currency(self, amount, from_currency, to_currency, rates):
        """Convert between international currencies"""
        # If rates are based on USD
        if from_currency == "USD":
            # Direct conversion from USD to target currency
            return amount * rates.get(to_currency, 1)
        elif to_currency == "USD":
            # Convert to USD
            return amount / rates.get(from_currency, 1)
        else:
            # Convert via USD
            usd_amount = amount / rates.get(from_currency, 1)
            return usd_amount * rates.get(to_currency, 1)

    def get_international_trend_data(self, currency, period_days):
        """Get trend data for international currency over a specified number of days"""
        try:
            # Calculate date range
            end_date = datetime.now()
            start_date = end_date - timedelta(days=period_days)
            
            # Initialize data structures
            dates = []
            rates = []
            
            # Fetch historical data
            # Note: You'll need to use a service that provides historical data
            # This is a simplified example using a free API
            url = f"https://api.exchangerate.host/timeseries?start_date={start_date.strftime('%Y-%m-%d')}&end_date={end_date.strftime('%Y-%m-%d')}&base=USD&symbols={currency}"
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            if "rates" in data:
                # Process the data
                for date_str, rate_data in sorted(data["rates"].items()):
                    if currency in rate_data:
                        dates.append(date_str)
                        rates.append(rate_data[currency])
            
            return {"dates": dates, "rates": rates}
        except Exception as e:
            print(f"Error fetching international trend data: {str(e)}")
            return {"dates": [], "rates": []}

    def generate_international_trend_chart(self, dates, rates, currency, period):
        """Generate a chart for international trend data"""
        # This function can be very similar to the original generate_trend_chart
        # Just change the title and labels to reflect international data
        
        # Create a temporary directory if it doesn't exist
        temp_dir = os.path.expanduser("~/.cache/ulauncher_eltoque")
        os.makedirs(temp_dir, exist_ok=True)
        
        # Create a unique filename
        filename = f"{temp_dir}/intl_{currency}_{period}_{int(time.time())}.png"
        
        try:
            # Create the chart (similar to original chart code)
            plt.figure(figsize=(10, 6))
            
            # Convert string dates to datetime objects
            datetime_dates = [datetime.strptime(date, "%Y-%m-%d") for date in dates]
            
            # Plot the data
            plt.plot(datetime_dates, rates, marker='o', linestyle='-', color='#1f77b4')
            
            # Set title and labels
            plt.title(f"{currency} to USD International Exchange Rate Trend ({period})")
            plt.xlabel("Date")
            plt.ylabel(f"Rate (1 USD to {currency})")
            plt.grid(True, linestyle='--', alpha=0.7)
            
            # Configure x-axis date formatting (same as original)
            # ...
            
            plt.tight_layout()
            
            # Save the chart
            plt.savefig(filename, dpi=100)
            plt.close()
            
            return filename
        except Exception as e:
            print(f"Error generating international trend chart: {str(e)}")
            return None

    def handle_rate_comparison(self, query, extension):
        """Compare ElToque rates with international market rates"""
        items = []
        
        try:
            # Get ElToque rates
            eltoque_data = self.fetch_exchange_rates(extension, datetime.now().strftime("%Y-%m-%d"))
            eltoque_rates = eltoque_data.get("tasas", {})
            
            # Get international rates
            international_rates = self.fetch_international_rates()
            
            if not eltoque_rates or not international_rates:
                items.append(ExtensionResultItem(
                    icon='images/compare.png',
                    name="Data Unavailable",
                    description="Could not fetch data from one or both sources.",
                    on_enter=CopyToClipboardAction("Data Unavailable")
                ))
                return RenderResultListAction(items)
            
            # Get USD to CUP rate from ElToque as reference
            usd_cup_rate = eltoque_rates.get("USD", 1)
            
            # Check if a specific currency is requested
            specific_currency = query.strip().upper() if query.strip() else None
            
            # Add header
            items.append(ExtensionResultItem(
                icon='images/compare.png',
                name="Rate Comparison: ElToque vs International",
                description=f"Reference: 1 USD = {usd_cup_rate:.2f} CUP",
                on_enter=CopyToClipboardAction(f"Reference: 1 USD = {usd_cup_rate:.2f} CUP")
            ))
            
            # Currencies to compare (use specific currency if provided)
            currencies_to_compare = [specific_currency] if specific_currency else ["EUR", "MLC", "USDT_TRC20"]
            
            for currency in currencies_to_compare:
                # Skip if currency not in ElToque rates
                if currency not in eltoque_rates and currency != "EUR":
                    continue
                
                # Handle EUR special case (ECU in ElToque)
                eltoque_currency = "ECU" if currency == "EUR" else currency
                
                # Get ElToque rate
                eltoque_rate = eltoque_rates.get(eltoque_currency, 0)
                if eltoque_rate == 0:
                    continue
                
                # Calculate ElToque USD equivalent
                eltoque_usd_equivalent = eltoque_rate / usd_cup_rate
                
                # Get international rate (USD to Currency)
                international_rate = 0
                if currency == "EUR":
                    international_rate = international_rates.get("EUR", 0)
                elif currency == "USDT_TRC20":
                    international_rate = 1  # USDT is pegged to USD
                elif currency == "MLC":
                    international_rate = 1  # MLC is theoretically 1:1 with USD
                
                # Skip if international rate is not available
                if international_rate == 0:
                    continue
                
                # Calculate difference
                difference = eltoque_usd_equivalent - international_rate
                difference_pct = (difference / international_rate) * 100 if international_rate != 0 else 0
                
                # Determine if ElToque rate is higher or lower
                if difference > 0:
                    comparison = "higher"
                    icon = "images/up.png"
                elif difference < 0:
                    comparison = "lower"
                    icon = "images/down.png"
                else:
                    comparison = "equal"
                    icon = "images/flat.png"
                
                # Display name for currency
                display_currency = extension.currency_names.get(eltoque_currency, currency)
                
                # Add comparison item
                items.append(ExtensionResultItem(
                    icon=icon,
                    name=f"{display_currency}: ElToque vs International",
                    description=f"ElToque: ${eltoque_usd_equivalent:.4f} | Int'l: ${international_rate:.4f} | Diff: {difference_pct:.2f}% {comparison}",
                    on_enter=CopyToClipboardAction(f"{display_currency} - ElToque: ${eltoque_usd_equivalent:.4f} | International: ${international_rate:.4f} | Difference: {difference_pct:.2f}%")
                ))
            
        except Exception as e:
            items.append(ExtensionResultItem(
                icon='images/compare.png',
                name="Error",
                description=str(e),
                on_enter=CopyToClipboardAction(str(e))
            ))
        
        return RenderResultListAction(items)

if __name__ == '__main__':
    ElToqueExtension().run()
