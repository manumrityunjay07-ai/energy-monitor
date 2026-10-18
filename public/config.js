/**
 * Energy Monitor Dashboard — Static Site Data Source Configuration
 * Configured for GitHub Pages public static deployment.
 *
 * Supported data sources:
 *   1. 'sample': Loads bundled benchmark/sample dataset (./data/sample_dashboard_data.json)
 *   2. 'custom': Loads from a custom pre-generated JSON URL (set customDataUrl or use ?data=<url>)
 *   3. 'local':  Attempts localhost /api/dashboard-data when running locally
 *
 * Database credentials never enter browser JavaScript.
 */
window.ENERGY_CONFIG = {
  // Default data source mode: 'sample' loads safe benchmark data for demonstration
  dataSource: 'sample',

  // Relative path to pre-generated sample data for GitHub Pages
  sampleDataUrl: './data/sample_dashboard_data.json',

  // Optional custom pre-generated results URL (can also be passed via URL query ?data=https://...)
  customDataUrl: '',

  // Local Python API endpoint (active only when running on localhost)
  localApiUrl: '/api/dashboard-data',

  // Polling / refresh interval in milliseconds (default 5 minutes)
  refreshIntervalMs: 5 * 60 * 1000,

  // Display timezone for all timestamps and hourly curves
  timezone: 'Asia/Kolkata',

  // Metadata
  siteMode: 'GitHub Pages Static Demo',
  deviceId: 153,
  version: '2026-10-18-pages',
};
