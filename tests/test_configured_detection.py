from backend.backends import detect


def test_configured_scan_uses_configured_port_and_reports_missing_weights(monkeypatch):
    config = {
        "comfyui_ports": {"image": 8210},
        "models": {"image": {"qwen21_ref": {"checkpoint": "qwen_image_2.1.safetensors"}}},
        "stages": {"image": {"default_model": "qwen21_ref"}},
        "agent": {"provider": "strata", "strata_url": "http://localhost:8211/v1",
                  "strata_model": "configured-model"},
    }
    from backend.comfy_client import ComfyUIClient
    monkeypatch.setattr(ComfyUIClient, "build_workflow", lambda *args: {"prompt": {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_2.1.safetensors"}},
        "2": {"class_type": "MissingNode"},
    }})

    def get_json(url, **kwargs):
        if url == "http://127.0.0.1:8210/object_info":
            return {"UNETLoader": {"input": {"required": {"unet_name": [["other.gguf"]]}}}}
        assert url == "http://localhost:8211/v1/models"
        return {"data": [{"id": "different-model"}]}

    monkeypatch.setattr(detect, "_get_json", get_json)
    image, chat = detect.inspect_configured(config)
    assert "MissingNode" in image["detail"]
    assert "qwen_image_2.1.safetensors" in image["detail"]
    assert "configured-model" in chat["detail"]
