
# Dynamics 365 Contact Center Tools

Practical utilities and guides for Dynamics 365 Contact Center operations — phone number migration, media archival, data import, and performance optimization.

## Tools

### [ACS to TPE Migration](acs-to-tpe-migration/)
PowerShell tool to bulk-migrate phone numbers from Azure Communication Services (ACS) Direct Routing to Teams Phone Extensibility (TPE) for Dynamics 365 Contact Center. Supports dry-run, validation, throttling, and auto-rollback.

### [D365 Media Archiver](d365-media-archiver/)
Python utility to export audio recordings, screen recordings, and transcripts from Dynamics 365 Contact Center to Azure Blob Storage. Reduces storage costs by ~10,000x by moving data from Dataverse to Azure Archive tier with automated lifecycle policies.

### [D365 Transcript Loader](d365-transcript-loader/)
Python script to import historical conversation transcripts from third-party systems (Zendesk, Genesys, Salesforce, etc.) into Dynamics 365 Contact Center via the Dataverse Web API.

## Getting Started

Each tool has its own README with prerequisites and usage instructions. Navigate to the tool folder for details.

## Contributing

Contributions welcome. Submit pull requests with clear descriptions.

## License

MIT License

## Disclaimer

This project is **not supported, endorsed, or managed by Microsoft**. These tools are provided as-is, without warranty of any kind. Use at your own risk. Always test in a non-production environment before applying to production systems.



