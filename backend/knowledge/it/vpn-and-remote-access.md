---
title: VPN and Remote Access
department: it
owner: IT Network Services
version: 2
---
# VPN and Remote Access

## Connecting to the VPN
Use the GlobalProtect client, portal vpn.acme.example. Sign in with your SSO credentials and approve the MFA prompt. The VPN is required for SAP, internal file shares and the finance systems; Microsoft 365, Slack and Zoom work without it.

## Troubleshooting VPN connection failures
First check that your internet connection works without the VPN. Then quit GlobalProtect completely and restart it. If you see "Gateway not responding", switch the gateway to the nearest region in GlobalProtect settings. If you see "Authentication failed", your SSO password may have expired: reset it and try again. Clearing the GlobalProtect cache (Settings > Troubleshooting > Reset) fixes most persistent errors.

## Home network requirements
The VPN needs outbound UDP 4501 and TCP 443. Some hotel and airport networks block UDP; GlobalProtect automatically falls back to TCP 443, which is slower but works.

## When to raise a ticket
If the VPN still fails after resetting GlobalProtect and confirming your password works, raise a ticket with a screenshot of the error and the output of Settings > Troubleshooting > Collect Logs.
