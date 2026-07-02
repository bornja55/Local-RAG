from streamlit.testing.v1 import AppTest
import time
import sys

print("Initializing AppTest for app.py...")
try:
    at = AppTest.from_file("app.py", default_timeout=300)
    print("Running initial app load...")
    at.run()
    print("Initial load complete. Checking if chat input exists...")
    
    if len(at.chat_input) > 0:
        chat_box = at.chat_input[0]
        print("Typing message into chat input: 'สวัสดีครับ นโยบายบริษัทมีอะไรบ้าง'")
        chat_box.set_value("สวัสดีครับ นโยบายบริษัทมีอะไรบ้าง").run()
        
        print("Waiting for response...")
        # Check the last chat message
        if len(at.chat_message) > 0:
            messages = at.chat_message
            last_message = messages[-1]
            print(f"Role: {last_message.name}")
            print("Content:")
            for item in last_message.markdown:
                print(item.value)
            print("\nSIMULATION SUCCESS!")
        else:
            print("No chat messages found after submitting.")
    else:
        print("Could not find chat_input widget. Did the app crash?")
        if at.exception:
            print("App Exception:", at.exception)
            
except Exception as e:
    print(f"Simulation failed: {e}")
