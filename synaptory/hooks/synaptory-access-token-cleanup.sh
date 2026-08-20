#!/usr/bin/env bash
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
#
# Hook: Stop / SessionEnd
# Purpose: No-op. The CLI owns keychain and session lifecycle; there is nothing
#          to clean up at hook level. Registered in hooks.json for compatibility.

exit 0
