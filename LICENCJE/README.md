# Software notices and source-availability evidence

Copies under notices/ are byte-for-byte text found in actual candidate files or
inside actual JARs. notice-index.csv maps original path/JAR entry to copy/hash.
Files are stored by hash to avoid Windows LICENSE/file-vs-license/directory
case collisions. No downloaded license has been disguised as a shipped notice.

Important: shaded JAR root LICENSE text can belong to a dependency, not the
main project. Upstream OpenTripPlanner v2.10.0 LICENSE states LGPL3-or-later;
upstream r5py/r5 v7.5.1-r5py LICENSE states MIT. Both JARs also contain Apache
dependency licenses. External reference links are checked separately below;
the BOM distinguishes upstream declarations from actual included texts.

OTP: artifact/version and embedded Maven components are in software-bom.csv.
No project patch recipe was found for otp-shaded-2.10.0.jar. A pinned download
does not alone prove a complete corresponding-source offer. No dedicated OTP
source bundle/written offer was found in the candidate. Need review of upstream
project notice/full license and source-delivery arrangements; not certified here.

R5: actual derivative contains its modified Java source and MIT text at
META-INF/wm-r5-instant-point/. The narrow change is the exact one-minute,
single-draw, non-frequency departure sampler guard; query times/calendars and
graphs are not rewritten by this patch. The actual embedded source, recipe,
binary-entry difference list and, when the pinned original is available, unified
diff are included as evidence. This is not a claim of a complete R5 source tree.
See r5-patch-evidence.json for observed differences and original source status.

JDK: actual release file identifies Eclipse Adoptium Temurin25.0.4.1+1-LTS.
Actual legal/ notices are copied. SOURCE/BUILD_SOURCE fields identify vendor
build references; no full JDK corresponding-source archive or separate source
offer was found in this candidate. Native binary versions left unknown in BOM
are not inferred from ABI names or lockfiles.

OSM: the recipient would receive machine-readable derivative databases, e.g.
map-context.sqlite, osm-*.geojson, family-ux.geojson, ux-mobility.sqlite and
prepared networks/state. File paths and schemas are inventoried. Raw PBF is not
included. No separate public ODbL database/change-set download offer or full
rebuild source distribution was found. Dataset licensing/combined contributions
must be assessed from these facts; this audit does not certify compliance.

Fonts/icons: no standalone font binaries were found in the runtime inventory.
Map/CSS request host Arial/sans-serif; JDK font resources are part of the JDK.
Web dependency licenses come from actual apps/web/dist/.vite/license.md.
Owner-approved SVG/emblem/icon files exist; an explicit owner redistribution
license is not present in the candidate. Do not infer it from visual approval.

Actual Sources view: sources-page-noise.html/txt is the rendered candidate's
MSIP noise source dialog. sources-api-selected-fields.json records actual API
metadata with data-record fields omitted. It is not an invented complete new
source page and does not close missing new-data provenance receipts.

External primary references checked2026-11-05 (not copies shipped in candidate):
- https://raw.githubusercontent.com/opentripplanner/OpenTripPlanner/v2.10.0/LICENSE
- https://raw.githubusercontent.com/r5py/r5/v7.5.1-r5py/LICENSE
- https://msip.krakow.pl/?dok_id=228972
- https://eli.gov.pl/api/acts/DU/2023/1524/text.html
- https://opendatacommons.org/licenses/odbl/1-0/
MSIP rules distinguish portal map services and contain conditions for organized
redistribution. Static ZIP acquisition and REST geometry acquisition are listed
separately in provenance; no permission/ban is inferred from an empty ArcGIS
copyright field. No written source-specific consent was found in reviewed files.


Additional observed distinctions: the OTP archive contains an LGPL2.1 text under
images/nuvola/license.txt (artwork). It is not the upstream OTP3-or-later project
notice. A search of copied OTP notices found no own OpenTripPlanner LGPL notice;
this does not resolve source-offer obligations. The GIOS provider's published
numeric quota was not established from the Swagger shell retrieved in this
audit; no numeric allowance is invented. The client has no explicit global
rate limiter regardless of that unresolved provider-policy detail. Public
upstream authorship/contact text is retained only as part of required original
license notices; no private user/team contacts or data-record values are added.
