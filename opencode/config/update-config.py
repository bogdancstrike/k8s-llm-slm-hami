#!/usr/bin/env python3
import urllib.request
import json
import os

LITELLM_HOST = os.environ.get("LITELLM_HOST", "http://litellm.local.ro")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "sk-litellm-master-change-me")

def main():
    config_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(config_dir, "opencode.json")
    
    print(f"Fetching models from {LITELLM_HOST}/v1/models...")
    req = urllib.request.Request(
        f"{LITELLM_HOST}/v1/models",
        headers={"Authorization": f"Bearer {LITELLM_KEY}"}
    )
    
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
    except Exception as e:
        print(f"Error connecting to LiteLLM: {e}")
        return

    # Extract models
    models = {}
    for item in res_data.get("data", []):
        model_id = item.get("id")
        if model_id:
            # Create a pretty name, e.g. "gemma-1b-fast" -> "Gemma 1b Fast"
            pretty_name = " ".join([w.capitalize() for w in model_id.replace("-", " ").replace("_", " ").split()])
            models[model_id] = {"name": pretty_name}

    print(f"Found models: {list(models.keys())}")

    # Load existing or create new config
    config = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
          "litellm": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "LiteLLM",
            "options": {
              "baseURL": f"{LITELLM_HOST}/v1",
              "apiKey": LITELLM_KEY
            },
            "models": {}
          }
        }
    }
    
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                existing_config = json.load(f)
                if isinstance(existing_config, dict):
                    # Keep other configuration items if present, only update provider litellm
                    config = existing_config
        except Exception as e:
            print(f"Error reading existing config: {e}, overwriting...")

    # Ensure provider structure exists
    config.setdefault("provider", {})
    config["provider"].setdefault("litellm", {
        "npm": "@ai-sdk/openai-compatible",
        "name": "LiteLLM"
    })
    config["provider"]["litellm"].setdefault("options", {})
    config["provider"]["litellm"]["options"]["baseURL"] = f"{LITELLM_HOST}/v1"
    config["provider"]["litellm"]["options"]["apiKey"] = LITELLM_KEY
    config["provider"]["litellm"]["models"] = models

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        
    print(f"Successfully updated {config_path}")

if __name__ == "__main__":
    main()
