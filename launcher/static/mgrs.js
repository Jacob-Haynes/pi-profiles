"use strict";
// Latitude/longitude (WGS84) to an MGRS grid reference, for the map's cursor readout. UTM by the
// USGS (Snyder) series, good to well under a metre; the Norway and Svalbard zone exceptions; no UPS
// (polar areas return null). toMGRS(38.8895, -77.0352, 4) -> "18S UJ 2348 0648" (the USNG reference example,
// Washington Monument); 5 figures each way (the default) = 1 m.
(function (root) {
  const BANDS = "CDEFGHJKLMNPQRSTUVWXX";
  const COLS = ["ABCDEFGH", "JKLMNPQR", "STUVWXYZ"];
  const ROWS = "ABCDEFGHJKLMNPQRSTUV";
  const a = 6378137, f = 1 / 298.257223563, k0 = 0.9996;
  const e2 = f * (2 - f), ep2 = e2 / (1 - e2), e4 = e2 * e2, e6 = e4 * e2;
  const rad = Math.PI / 180;

  function zoneOf(lat, lon) {
    let z = Math.floor((lon + 180) / 6) + 1;
    if (z > 60) z = 60;
    if (lat >= 56 && lat < 64 && lon >= 3 && lon < 12) z = 32;
    if (lat >= 72 && lat < 84) {
      if (lon >= 0 && lon < 9) z = 31; else if (lon >= 9 && lon < 21) z = 33;
      else if (lon >= 21 && lon < 33) z = 35; else if (lon >= 33 && lon < 42) z = 37;
    }
    return z;
  }

  function toUTM(lat, lon) {
    const zone = zoneOf(lat, lon);
    const phi = lat * rad, lam = lon * rad, lam0 = ((zone - 1) * 6 - 180 + 3) * rad;
    const s = Math.sin(phi), c = Math.cos(phi), t = Math.tan(phi);
    const N = a / Math.sqrt(1 - e2 * s * s), T = t * t, C = ep2 * c * c, A = c * (lam - lam0);
    const M = a * ((1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi
      - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * Math.sin(2 * phi)
      + (15 * e4 / 256 + 45 * e6 / 1024) * Math.sin(4 * phi)
      - (35 * e6 / 3072) * Math.sin(6 * phi));
    const E = k0 * N * (A + (1 - T + C) * A ** 3 / 6 + (5 - 18 * T + T * T + 72 * C - 58 * ep2) * A ** 5 / 120) + 500000;
    let Nn = k0 * (M + N * t * (A * A / 2 + (5 - T + 9 * C + 4 * C * C) * A ** 4 / 24
      + (61 - 58 * T + T * T + 600 * C - 330 * ep2) * A ** 6 / 720));
    if (lat < 0) Nn += 10000000;
    return { zone, easting: E, northing: Nn };
  }

  function toMGRS(lat, lon, digits) {
    if (typeof lat !== "number" || typeof lon !== "number" || !isFinite(lat) || !isFinite(lon)) return null;
    if (lat < -80 || lat > 84) return null;
    if (lon >= 180) lon -= 360;
    digits = digits == null ? 5 : Math.max(1, Math.min(5, digits));
    const u = toUTM(lat, lon);
    const band = BANDS[Math.min(20, Math.floor((lat + 80) / 8))];
    const col = Math.floor(u.easting / 100000), row = Math.floor(u.northing / 100000) % 20;
    const colL = COLS[(u.zone - 1) % 3][col - 1];
    const rowL = ROWS[(row + (u.zone % 2 === 0 ? 5 : 0)) % 20];
    if (!colL) return null;
    const div = 10 ** (5 - digits);
    const e = String(Math.floor((u.easting % 100000) / div)).padStart(digits, "0");
    const n = String(Math.floor((u.northing % 100000) / div)).padStart(digits, "0");
    return u.zone + band + " " + colL + rowL + " " + e + " " + n;
  }

  const api = { toMGRS, toUTM };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.MGRS = api;
})(typeof window !== "undefined" ? window : this);
