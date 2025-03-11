import json
import requests
import time
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import io
import base64
import os
import sqlite3
from ulauncher.api.client.Extension import Extension
from ulauncher.api.client.EventListener import EventListener
from ulauncher.api.shared.event import KeywordQueryEvent, PreferencesUpdateEvent, PreferencesEvent
from ulauncher.api.shared.item.ExtensionResultItem import ExtensionResultItem
from ulauncher.api.shared.action.RenderResultListAction import RenderResultListAction
from ulauncher.api.shared.action.CopyToClipboardAction import CopyToClipboardAction
from ulauncher.api.shared.action.OpenAction import OpenAction

# Global variables for caching
CACHE_DURATION = 300  # Cache duration in seconds (5 minutes)
last_api_call_time = None
cached_data = None
cached_date = None  # Store the date for which data is cached
trend_cache = {}  # Cache for trend data {currency_period: {dates: [], rates: []}}

# Database configuration
DB_PATH = os.path.expanduser("~/.local/share/ulauncher_eltoque/rates.db")

class ElToqueExtension(Extension):
    def __init__(self):
        super().__init__()
        self.subscribe(KeywordQueryEvent, KeywordQueryEventListener())
        self.subscribe(PreferencesEvent, PreferencesEventListener())
        self.subscribe(PreferencesUpdateEvent, PreferencesUpdateEventListener())
        
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

class PreferencesEventListener(EventListener):
    def on_event(self, event, extension):
        # Load preferences when the extension starts
        extension.api_key = event.preferences.get('api_key', '')
        
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

class PreferencesUpdateEventListener(EventListener):
    def on_event(self, event, extension):
        # Update the API key if it changed
        if event.id == 'api_key':
            extension.api_key = event.new_value
        
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

class KeywordQueryEventListener(EventListener):
    def on_event(self, event, extension):
        global last_api_call_time, cached_data, cached_date, trend_cache

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
                        description="Please use the format: 'USD trend 7d' (supports 7d, 30d, 90d, 1y)",
                        on_enter=CopyToClipboardAction("Invalid Trend Query")
                    ))
                else:
                    currency_input = parts[0].upper()
                    period = parts[2].lower()
                    
                    # Convert user input currency to API currency
                    currency = extension.currency_aliases.get(currency_input, currency_input)
                    
                    # Validate the period
                    valid_periods = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
                    if period not in valid_periods:
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="Invalid Period",
                            description="Supported periods: 7d, 30d, 90d, 1y",
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
                            # Generate trend chart
                            chart_path = self.generate_trend_chart(trend_data["dates"], trend_data["rates"], currency_input, period)
                            
                            # Calculate statistics
                            current_rate = trend_data["rates"][-1] if trend_data["rates"] else 0
                            min_rate = min(trend_data["rates"]) if trend_data["rates"] else 0
                            max_rate = max(trend_data["rates"]) if trend_data["rates"] else 0
                            avg_rate = sum(trend_data["rates"]) / len(trend_data["rates"]) if trend_data["rates"] else 0
                            
                            # Calculate change
                            first_rate = trend_data["rates"][0] if trend_data["rates"] else 0
                            change = current_rate - first_rate
                            change_percent = (change / first_rate * 100) if first_rate else 0
                            change_sign = "+" if change >= 0 else ""
                            
                            # Add trend summary item
                            items.append(ExtensionResultItem(
                                icon='images/icon.png',
                                name=f"{currency_input} Trend ({period})",
                                description=f"Current: {current_rate:.2f} CUP | Change: {change_sign}{change:.2f} ({change_sign}{change_percent:.2f}%)",
                                on_enter=OpenAction(chart_path)
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
                                description=f"From {trend_data['dates'][0]} to {trend_data['dates'][-1]}",
                                on_enter=CopyToClipboardAction(f"From {trend_data['dates'][0]} to {trend_data['dates'][-1]}")
                            ))
            except Exception as e:
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name="Error Processing Trend",
                    description=str(e),
                    on_enter=CopyToClipboardAction(str(e))
                ))
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
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="No data available",
                            description=f"No exchange rates found for {target_date}.",
                            on_enter=CopyToClipboardAction("No data available")
                        ))
                    else:
                        # Check if currencies are supported (CUP is always valid)
                        valid_from = from_currency == "CUP" or from_currency in tasas
                        valid_to = to_currency == "CUP" or to_currency in tasas
                        if not valid_from or not valid_to:
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
                    items.append(ExtensionResultItem(
                        icon='images/icon.png',
                        name="Invalid Input",
                        description="Please use the format: '100 USD to EUR' or 'YYYY-MM-DD 100 USD to EUR'",
                        on_enter=CopyToClipboardAction("Invalid Input")
                    ))
                except requests.exceptions.HTTPError as e:
                    if e.response.status_code == 429:
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="Rate Limit Exceeded",
                            description="Please wait a few minutes before trying again.",
                            on_enter=CopyToClipboardAction("Rate Limit Exceeded")
                        ))
                    elif e.response.status_code == 401:
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="Invalid API Key",
                            description="Please check your API key in the extension settings.",
                            on_enter=CopyToClipboardAction("Invalid API Key")
                        ))
                    else:
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="API Error",
                            description=f"HTTP Error: {str(e)}",
                            on_enter=CopyToClipboardAction(str(e))
                        ))
                except Exception as e:
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
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="No data available",
                            description=f"No exchange rates found for {target_date}.",
                            on_enter=CopyToClipboardAction("No data available")
                        ))
                    else:
                        # Add a header item showing the date
                        if target_date != datetime.now().strftime("%Y-%m-%d"):
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
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="Rate Limit Exceeded",
                            description="Please wait a few minutes before trying again.",
                            on_enter=CopyToClipboardAction("Rate Limit Exceeded")
                        ))
                    elif e.response.status_code == 401:
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="Invalid API Key",
                            description="Please check your API key in the extension settings.",
                            on_enter=CopyToClipboardAction("Invalid API Key")
                        ))
                    else:
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
                        items.append(ExtensionResultItem(
                            icon='images/icon.png',
                            name="Network Error",
                            description=f"Failed to fetch data: {str(e)}",
                            on_enter=CopyToClipboardAction(str(e))
                        ))
                except json.JSONDecodeError as e:
                    items.append(ExtensionResultItem(
                        icon='images/icon.png',
                        name="JSON Error",
                        description=f"Invalid API response: {str(e)}",
                        on_enter=CopyToClipboardAction(str(e))
                    ))
                except Exception as e:
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
        all_rates = []
        missing_dates = []
        
        # First, get all dates in the range
        current_date = start_date
        while current_date <= end_date:
            all_dates.append(current_date.strftime("%Y-%m-%d"))
            current_date += timedelta(days=1)
        
        # Try to get data from the local database first
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Query the database for trend data
            cursor.execute(
                "SELECT date, rate FROM rates WHERE currency = ? AND date >= ? AND date <= ? ORDER BY date",
                (currency, start_date_str, end_date.strftime("%Y-%m-%d"))
            )
            db_results = cursor.fetchall()
            conn.close()
            
            # Create a dictionary of existing data
            db_data = {date: rate for date, rate in db_results}
            
            # Check which dates are missing
            for date_str in all_dates:
                if date_str in db_data:
                    all_rates.append(db_data[date_str])
                else:
                    all_rates.append(None)  # Placeholder for missing data
                    missing_dates.append(date_str)
                
        except Exception as e:
            print(f"Database error in get_trend_data: {str(e)}")
            # If database query fails, all dates are missing
            missing_dates = all_dates
            all_rates = [None] * len(all_dates)
        
        # Fetch missing data from API
        if missing_dates:
            print(f"Fetching {len(missing_dates)} missing dates from API for {currency}")
            for date_str in missing_dates:
                try:
                    data = self.fetch_exchange_rates(extension, date_str, force_api=True)
                    tasas = data.get("tasas", {})
                    
                    # If we have data for this currency on this date, update our lists
                    if currency in tasas:
                        idx = all_dates.index(date_str)
                        all_rates[idx] = tasas[currency]
                except Exception as e:
                    print(f"Error fetching data for {date_str}: {str(e)}")
                    # Keep the None value for this date
        
        # Remove any None values (dates with no data)
        valid_data = [(date, rate) for date, rate in zip(all_dates, all_rates) if rate is not None]
        
        if not valid_data:
            return {"dates": [], "rates": [], "timestamp": time.time()}
        
        # Unzip the valid data
        valid_dates, valid_rates = zip(*valid_data)
        
        # Cache the result
        result = {
            "dates": valid_dates,
            "rates": valid_rates,
            "timestamp": time.time()
        }
        trend_cache[cache_key] = result
        
        return result

    def generate_trend_chart(self, dates, rates, currency, period):
        """Generate a chart for the trend data and save it to a temporary file"""
        # Create a temporary directory if it doesn't exist
        temp_dir = os.path.expanduser("~/.cache/ulauncher_eltoque")
        os.makedirs(temp_dir, exist_ok=True)
        
        # Create a unique filename
        filename = f"{temp_dir}/{currency}_{period}_{int(time.time())}.png"
        
        # Create the chart
        plt.figure(figsize=(10, 6))
        plt.plot(dates, rates, marker='o', linestyle='-', color='#1f77b4')
        plt.title(f"{currency} to CUP Exchange Rate Trend ({period})")
        plt.xlabel("Date")
        plt.ylabel("Rate (CUP)")
        plt.grid(True, linestyle='--', alpha=0.7)
        plt.xticks(rotation=45)
        
        # Add some visual improvements
        if len(dates) > 1:
            # Add trend line
            plt.plot(dates, rates, 'r--', alpha=0.3)
            
            # Highlight min and max points
            min_rate = min(rates)
            max_rate = max(rates)
            min_idx = rates.index(min_rate)
            max_idx = rates.index(max_rate)
            
            plt.plot(dates[min_idx], min_rate, 'go', markersize=10)
            plt.plot(dates[max_idx], max_rate, 'ro', markersize=10)
            
            # Add annotations
            plt.annotate(f"Min: {min_rate:.2f}", 
                        (dates[min_idx], min_rate),
                        xytext=(10, -20),
                        textcoords="offset points",
                        arrowprops=dict(arrowstyle="->"))
            
            plt.annotate(f"Max: {max_rate:.2f}", 
                        (dates[max_idx], max_rate),
                        xytext=(10, 20),
                        textcoords="offset points",
                        arrowprops=dict(arrowstyle="->"))
        
        plt.tight_layout()
        
        # Save the chart
        plt.savefig(filename, dpi=100)
        plt.close()
        
        # Automatically open the chart
        try:
            # Try to open the file with the default image viewer
            import subprocess
            import platform
            
            system = platform.system()
            if system == 'Darwin':  # macOS
                subprocess.Popen(['open', filename])
            elif system == 'Linux':
                subprocess.Popen(['xdg-open', filename])
            elif system == 'Windows':
                subprocess.Popen(['start', filename], shell=True)
        except Exception as e:
            print(f"Error opening chart: {str(e)}")
        
        return filename

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
        
        # Basic usage
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="Basic Usage",
            description="Type the keyword alone to see current exchange rates",
            on_enter=CopyToClipboardAction("Basic Usage: Type the keyword alone to see current exchange rates")
        ))
        
        # Currency conversion
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="Currency Conversion",
            description="Example: '100 USD to EUR' or '50 MLC to USDT'",
            on_enter=CopyToClipboardAction("Currency Conversion: 100 USD to EUR")
        ))
        
        # Historical rates
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="Historical Rates",
            description="Example: '2024-03-01 100 USD to EUR' or 'history 2024-03-01'",
            on_enter=CopyToClipboardAction("Historical Rates: 2024-03-01 100 USD to EUR")
        ))
        
        # Trend analysis
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="Trend Analysis",
            description="Example: 'USD trend 7d' (supports 7d, 30d, 90d, 1y)",
            on_enter=CopyToClipboardAction("Trend Analysis: USD trend 7d")
        ))
        
        # Database commands
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="Database Management",
            description="Commands: 'db status', 'db clear', 'db backup', 'db restore', 'db rebuild'",
            on_enter=CopyToClipboardAction("Database Management: db status")
        ))
        
        # History lookup
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="History Lookup",
            description="Example: 'history 2024-03-01' or 'history 2024-03-01 USD'",
            on_enter=CopyToClipboardAction("History Lookup: history 2024-03-01 USD")
        ))
        
        # Help command
        items.append(ExtensionResultItem(
            icon='images/icon.png',
            name="Help Command",
            description="Type 'help' or '?' to show this help information",
            on_enter=CopyToClipboardAction("Help Command: help")
        ))
        
        return RenderResultListAction(items)

if __name__ == '__main__':
    ElToqueExtension().run()
