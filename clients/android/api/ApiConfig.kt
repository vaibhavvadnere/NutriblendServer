package com.nutriblend.api

/**
 * Where the API lives.
 *
 * IMPORTANT — "localhost" does not mean your Mac when the code runs on a phone
 * or emulator. It means the phone itself, so you will get "connection refused".
 *
 *   Android emulator  ->  http://10.0.2.2:8000     (10.0.2.2 is the host machine)
 *   Real device       ->  http://192.168.x.x:8000  (your Mac's LAN IP, same wifi;
 *                                                   find it with: ipconfig getifaddr en0)
 *   Deployed          ->  https://api.yourdomain.com
 *
 * Note the trailing slash — Retrofit requires it on the base URL.
 */
object ApiConfig {

    // TODO: point this at your machine, then at your deployed server.
    const val BASE_URL = "http://10.0.2.2:8000/"

    const val API_PREFIX = "api/v1"

    /**
     * Plain http:// is blocked by Android 9+ unless you allow it. For local
     * development add a network security config that permits cleartext to your
     * dev host only — never `android:usesCleartextTraffic="true"` app-wide,
     * which would also allow it in your release build.
     *
     * res/xml/network_security_config.xml:
     *
     *   <network-security-config>
     *       <domain-config cleartextTrafficPermitted="true">
     *           <domain includeSubdomains="true">10.0.2.2</domain>
     *           <domain includeSubdomains="true">192.168.1.5</domain>
     *       </domain-config>
     *   </network-security-config>
     *
     * AndroidManifest.xml, inside <application>:
     *   android:networkSecurityConfig="@xml/network_security_config"
     */
}
