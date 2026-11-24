"""内置样例：一键载入/试跑两种典型情形。

样例A「保留产物」：容量刚好装下全部产物，规划应 0 删除 0 重跑。
样例B「删后重建」：容量吃紧，extract_keys 必须在 pack_image 构建前删除、
其后为 seal_device 重建，规划应恰好 1 次重跑、峰值顶到上限。
"""

SAMPLES = {
    "a": {
        "title": "样例A · 保留产物（容量充足，0 删除 0 重跑）",
        "capacity": 1500000,
        "rerun_limit": 0,
        "target": "link_firmware",
        "steps": [
            {"name": "fetch_sources", "size": 200000, "deps": ""},
            {"name": "toolchain", "size": 300000, "deps": ""},
            {"name": "compile_boot", "size": 250000, "deps": "fetch_sources"},
            {"name": "compile_kernel", "size": 400000,
             "deps": "fetch_sources,toolchain"},
            {"name": "link_firmware", "size": 350000,
             "deps": "compile_boot,compile_kernel"},
        ],
    },
    "b": {
        "title": "样例B · 删后重建（容量吃紧，恰好 1 次重跑）",
        "capacity": 800000,
        "rerun_limit": 1,
        "target": "seal_device",
        "steps": [
            {"name": "extract_keys", "size": 300000, "deps": ""},
            {"name": "sign_bootloader", "size": 200000, "deps": "extract_keys"},
            {"name": "pack_image", "size": 400000, "deps": "sign_bootloader"},
            {"name": "seal_device", "size": 100000,
             "deps": "extract_keys,pack_image"},
        ],
    },
}
