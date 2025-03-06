import json
import requests
import time
from datetime import datetime, timedelta
from ulauncher.api.client.Extension import Extension
from ulauncher.api.client.EventListener import EventListener
from ulauncher.api.shared.event import KeywordQueryEvent, PreferencesUpdateEvent, PreferencesEvent
from ulauncher.api.shared.item.ExtensionResultItem import ExtensionResultItem
from ulauncher.api.shared.action.RenderResultListAction import RenderResultListAction
from ulauncher.api.shared.action.CopyToClipboardAction import CopyToClipboardAction

# Global variables for caching
CACHE_DURATION = 300  # Cache duration in seconds (5 minutes)
last_api_call_time = None
cached_data = None

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
        global last_api_call_time, cached_data

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

                # Fetch exchange rates from ElToque API (with caching)
                now = time.time()
                if cached_data and last_api_call_time and (now - last_api_call_time) < CACHE_DURATION:
                    data = cached_data
                else:
                    today = datetime.now().strftime("%Y-%m-%d")
                    date_from = f"{today} 00:00:01"
                    date_to = f"{today} 23:59:01"
                    url = f"https://tasas.eltoque.com/v1/trmi?date_from={date_from}&date_to={date_to}"
                    headers = {
                        "accept": "*/*",
                        "Authorization": f"Bearer {extension.api_key}"
                    }
                    response = requests.get(url, headers=headers)
                    response.raise_for_status()  # Raise an exception for HTTP errors
                    data = response.json()
                    cached_data = data  # Cache the response
                    last_api_call_time = now  # Update the last API call time

                # Extract exchange rates
                tasas = data.get("tasas", {})
                if not tasas:
                    items.append(ExtensionResultItem(
                        icon='images/icon.png',
                        name="No data available",
                        description="No exchange rates found for today.",
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
                        items.append(ExtensionResultItem(
                            icon=from_icon,
                            name=f"{amount} {from_display} = {result:.2f} {to_display}",
                            description=f"Exchange rate: 1 {from_display} = {from_rate / to_rate:.2f} {to_display}",
                            on_enter=CopyToClipboardAction(str(result))
                        ))

            except (IndexError, ValueError):
                items.append(ExtensionResultItem(
                    icon='images/icon.png',
                    name="Invalid Input",
                    description="Please use the format: '100 USD to EUR'.",
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
                now = time.time()
                if cached_data and last_api_call_time and (now - last_api_call_time) < CACHE_DURATION:
                    data = cached_data
                else:
                    today = datetime.now().strftime("%Y-%m-%d")
                    date_from = f"{today} 00:00:01"
                    date_to = f"{today} 23:59:01"
                    url = f"https://tasas.eltoque.com/v1/trmi?date_from={date_from}&date_to={date_to}"
                    headers = {
                        "accept": "*/*",
                        "Authorization": f"Bearer {extension.api_key}"
                    }
                    response = requests.get(url, headers=headers)
                    response.raise_for_status()  # Raise an exception for HTTP errors
                    data = response.json()
                    cached_data = data  # Cache the response
                    last_api_call_time = now  # Update the last API call time

                # Extract exchange rates from the response
                tasas = data.get("tasas", {})
                if not tasas:
                    items.append(ExtensionResultItem(
                        icon='images/icon.png',
                        name="No data available",
                        description="No exchange rates found for today.",
                        on_enter=CopyToClipboardAction("No data available")
                    ))
                else:
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

if __name__ == '__main__':
    ElToqueExtension().run()
