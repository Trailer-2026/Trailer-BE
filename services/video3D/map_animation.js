const MAP_STYLE = "mapbox://styles/mapbox/standard";
// 지도 테마 (window.MAP_THEME 로 주입). "winter": dusk 조명 + 눈 파티클.
const MAP_THEME = String(window.MAP_THEME || "").toLowerCase();
const TERRAIN_SOURCE_ID = "mapbox-dem";
const FULL_ROUTE_SOURCE_ID = "route-planned";
const PROGRESS_ROUTE_SOURCE_ID = "route-progress";
const CURRENT_SOURCE_ID = "current-position";
const TRAIN_ICON_ID = "train-icon";
const TRAIN_MODEL_ID = "train-model";
// train_color.glb: assets/Little train (OBJ+MTL) 를 텍스처 포함 GLB 로 변환한
// 컬러 모델 (길이 1.899 로 기존 규격과 동일). train_fixed.glb 는 원본
// train.glb 에 법선/재질을 보정한 무채색 폴백 (Mapbox 는 법선 없는 GLB 를
// 렌더링하지 못한다). 앞에서부터 로드에 성공한 파일을 사용.
const TRAIN_MODEL_URLS = [
  "./assets/train_color.glb",
  "./assets/train_fixed.glb",
  "./assets/train.glb"
];
const TRAIN_MODEL_LAYER_ID = "current-position-train-model";
// 줌이 1 내려갈 때마다 스케일을 2배로 키워 화면상 크기를 일정하게 유지한다.
const TRAIN_MODEL_REFERENCE_ZOOM = 15.5;
// 기준 줌(15.5)에서의 모델 배율. train.glb 원본이 1.9m 길이라 75배 ≈ 143m.
const TRAIN_MODEL_BASE_SCALE = 75;
// 모델 기수(기관차 굴뚝 쪽) 방향 보정치. rot 0 에서 기수가 서쪽(270°)을 보고,
// model-rotation 의 +z 는 위에서 봤을 때 시계방향(나침반 방위와 동일)이라
// 기수를 bearing 으로 돌리려면 z = bearing + 90.
const TRAIN_MODEL_HEADING_OFFSET_DEG = 90;
const MARKER_SOURCE_ID = "route-markers";
const DESTINATION_LABEL_SOURCE_ID = "destination-label";
const DEFAULT_RENDER_FPS = 30;
const BEARING_MAX_DEGREES_PER_SECOND = 45;
const BEARING_SMOOTHING_SPEED = 3.0;
const BEARING_LOOK_AHEAD_POINTS = 10;
const BEARING_TARGET_HISTORY_SIZE = 5;
const DEBUG_BEARING_INTERVAL_FRAMES = 15;

const defaultRoutePoints = [
  {
    name: "서울역",
    longitude: 126.9706,
    latitude: 37.5547
  },
  {
    name: "대전",
    longitude: 127.3845,
    latitude: 36.3504
  },
  {
    name: "대구",
    longitude: 128.6014,
    latitude: 35.8714
  },
  {
    name: "부산역",
    longitude: 129.0403,
    latitude: 35.1151
  }
];

const bearingTestRoutePoints = [
  {
    name: "Bearing test start",
    longitude: 127.0100,
    latitude: 37.5400
  },
  {
    name: "East turn",
    longitude: 127.0750,
    latitude: 37.5400
  },
  {
    name: "South turn",
    longitude: 127.0750,
    latitude: 37.4850
  },
  {
    name: "West turn",
    longitude: 127.0050,
    latitude: 37.4850
  },
  {
    name: "Southeast finish",
    longitude: 127.0550,
    latitude: 37.4450
  }
];

function travelDataFromRoutePoints(points) {
  return {
    trackPoints: points.map((point) => ({
      latitude: point.latitude,
      longitude: point.longitude
    })),
    mediaPoints: [
      {
        trackIndex: 0,
        name: points[0].name,
        photos: []
      },
      {
        trackIndex: points.length - 1,
        name: points[points.length - 1].name,
        photos: ["assets/destination_photo.jpg"]
      }
    ]
  };
}

function normalizeTrackPoints(rawTrackPoints) {
  if (!Array.isArray(rawTrackPoints)) {
    return [];
  }

  return rawTrackPoints
    .map((point) => ({
      latitude: Number(point.latitude),
      longitude: Number(point.longitude),
      timestamp: point.timestamp || null
    }))
    .filter(
      (point) =>
        Number.isFinite(point.latitude) &&
        Number.isFinite(point.longitude) &&
        Math.abs(point.latitude) <= 90 &&
        Math.abs(point.longitude) <= 180
    );
}

function normalizeMediaPoints(rawMediaPoints, trackPointCount) {
  if (!Array.isArray(rawMediaPoints)) {
    return [];
  }

  return rawMediaPoints
    .map((point) => ({
      trackIndex: Number(point.trackIndex),
      name: typeof point.name === "string" ? point.name : "",
      photos: Array.isArray(point.photos) ? point.photos : []
    }))
    .filter(
      (point) =>
        Number.isInteger(point.trackIndex) &&
        point.trackIndex >= 0 &&
        point.trackIndex < trackPointCount
    )
    .sort((a, b) => a.trackIndex - b.trackIndex);
}

function getInitialTravelData() {
  if (window.TRAVEL_DATA) {
    return window.TRAVEL_DATA;
  }
  if (window.USE_BEARING_TEST_ROUTE) {
    return travelDataFromRoutePoints(bearingTestRoutePoints);
  }
  return travelDataFromRoutePoints(defaultRoutePoints);
}

const initialTravelData = getInitialTravelData();
const normalizedTrackPoints = normalizeTrackPoints(initialTravelData.trackPoints);
const routePoints =
  normalizedTrackPoints.length >= 2
    ? normalizedTrackPoints
    : normalizeTrackPoints(travelDataFromRoutePoints(defaultRoutePoints).trackPoints);
const mediaPoints = normalizeMediaPoints(
  initialTravelData.mediaPoints,
  routePoints.length
);

let map = null;
let renderReady = false;
let trainModelActive = false;
let routeCoordinates = [];
let routeDistances = [];
let trackDistances = [];
let totalRouteDistanceKm = 0;
let initialBearing = 145;
let finalBearing = 145;
let smoothedBearing = null;
let lastProgress = null;
let renderFrameCounter = 0;
let targetBearingHistory = [];
// 카메라 연속성 상태: 마지막으로 적용한 카메라와, 현재 세그먼트에 들어올 때의
// 카메라. 세그먼트가 바뀌는 순간(이동<->정지, 정지 후 재출발, 구간 길이가 달라
// 순항 줌이 달라질 때) 이전 카메라에서 목표 카메라로 이어서 보간해 줌/중심이
// 튀지 않게 한다. null 이면 첫 세그먼트(기존 연출 유지).
let lastCameraState = null; // { zoom, ahead, pitch, center }
let moveEntryState = null;
let moveEntryKey = null;
let stopEntryState = null;
let stopEntryKey = null;

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function lerp(a, b, t) {
  return a + (b - a) * t;
}

function easeInOut(t) {
  const x = clamp(t, 0, 1);
  return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
}

function normalizeAngle(angle) {
  return ((angle % 360) + 360) % 360;
}

function shortestAngleDelta(from, to) {
  return ((to - from + 540) % 360) - 180;
}

function lerpAngle(from, to, t) {
  const delta = shortestAngleDelta(from, to);
  return normalizeAngle(from + delta * t);
}

function interpolateAngle(a, b, t) {
  return lerpAngle(a, b, t);
}

function toRadians(degrees) {
  return (degrees * Math.PI) / 180;
}

function toDegrees(radians) {
  return (radians * 180) / Math.PI;
}

function haversineKm(a, b) {
  const radiusKm = 6371;
  const lat1 = toRadians(a[1]);
  const lat2 = toRadians(b[1]);
  const dLat = toRadians(b[1] - a[1]);
  const dLon = toRadians(b[0] - a[0]);
  const h =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(lat1) *
      Math.cos(lat2) *
      Math.sin(dLon / 2) *
      Math.sin(dLon / 2);
  return 2 * radiusKm * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h));
}

function bearingBetween(a, b) {
  const lat1 = toRadians(a[1]);
  const lat2 = toRadians(b[1]);
  const dLon = toRadians(b[0] - a[0]);
  const y = Math.sin(dLon) * Math.cos(lat2);
  const x =
    Math.cos(lat1) * Math.sin(lat2) -
    Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
  return normalizeAngle(toDegrees(Math.atan2(y, x)));
}

function circularMean(angles) {
  let sinSum = 0;
  let cosSum = 0;

  for (const angle of angles) {
    const radians = toRadians(angle);
    sinSum += Math.sin(radians);
    cosSum += Math.cos(radians);
  }

  return normalizeAngle(toDegrees(Math.atan2(sinSum, cosSum)));
}

function smoothingFactor(speed, deltaSeconds) {
  return 1 - Math.exp(-speed * deltaSeconds);
}

function moveAngleToward(current, target, maxDelta) {
  const delta = shortestAngleDelta(current, target);

  if (Math.abs(delta) <= maxDelta) {
    return normalizeAngle(target);
  }

  return normalizeAngle(current + Math.sign(delta) * maxDelta);
}

function getRenderFps() {
  const fps = Number(window.RENDER_FPS);
  return Number.isFinite(fps) && fps > 0 ? fps : DEFAULT_RENDER_FPS;
}

function resetBearingSmoothing() {
  smoothedBearing = null;
  targetBearingHistory = [];
}

window.resetAnimationState = function () {
  smoothedBearing = null;
  lastProgress = null;
  renderFrameCounter = 0;
  targetBearingHistory = [];
  return true;
};

function stableTargetBearing(targetBearing) {
  const normalized = normalizeAngle(targetBearing);
  targetBearingHistory.push(normalized);

  if (targetBearingHistory.length > BEARING_TARGET_HISTORY_SIZE) {
    targetBearingHistory.shift();
  }

  return circularMean(targetBearingHistory);
}

function smoothBearingForFrame(
  progress,
  targetBearing,
  routeIndex,
  lookAheadIndex,
  allowLargeStep = false
) {
  if (
    lastProgress !== null &&
    (progress < lastProgress - 0.000001 ||
      (!allowLargeStep && progress - lastProgress > 0.08))
  ) {
    resetBearingSmoothing();
  }

  const fps = getRenderFps();
  const deltaSeconds = 1 / fps;
  const maxDeltaPerFrame = BEARING_MAX_DEGREES_PER_SECOND / fps;
  const normalizedTarget = normalizeAngle(targetBearing);
  const averagedTarget = stableTargetBearing(normalizedTarget);

  if (smoothedBearing === null) {
    smoothedBearing = averagedTarget;
    lastProgress = progress;
    return {
      bearing: smoothedBearing,
      targetBearing: averagedTarget,
      rawTargetBearing: normalizedTarget,
      angleDelta: 0,
      maxDeltaPerFrame,
      routeIndex,
      lookAheadIndex
    };
  }

  const previousBearing = smoothedBearing;
  const factor = smoothingFactor(BEARING_SMOOTHING_SPEED, deltaSeconds);
  const smoothedCandidate = lerpAngle(previousBearing, averagedTarget, factor);
  smoothedBearing = moveAngleToward(
    previousBearing,
    smoothedCandidate,
    maxDeltaPerFrame
  );
  const angleDelta = shortestAngleDelta(previousBearing, averagedTarget);
  lastProgress = progress;

  if (
    window.DEBUG_BEARING &&
    renderFrameCounter % DEBUG_BEARING_INTERVAL_FRAMES === 0
  ) {
    console.info(
      `[bearing] progress=${progress.toFixed(4)} ` +
        `target=${averagedTarget.toFixed(2)} ` +
        `smoothed=${smoothedBearing.toFixed(2)} ` +
        `delta=${angleDelta.toFixed(2)} ` +
        `maxDelta=${maxDeltaPerFrame.toFixed(2)} ` +
        `idx=${routeIndex ?? "n/a"} ` +
        `lookAhead=${lookAheadIndex ?? "n/a"}`
    );
  }

  return {
    bearing: smoothedBearing,
    targetBearing: averagedTarget,
    rawTargetBearing: normalizedTarget,
    angleDelta,
    maxDeltaPerFrame,
    routeIndex,
    lookAheadIndex
  };
}

function offsetCoordinate(coord, bearingDeg, distanceMeters) {
  const radiusMeters = 6371000;
  const bearing = toRadians(bearingDeg);
  const lat1 = toRadians(coord[1]);
  const lon1 = toRadians(coord[0]);
  const angularDistance = distanceMeters / radiusMeters;

  const lat2 = Math.asin(
    Math.sin(lat1) * Math.cos(angularDistance) +
      Math.cos(lat1) * Math.sin(angularDistance) * Math.cos(bearing)
  );
  const lon2 =
    lon1 +
    Math.atan2(
      Math.sin(bearing) * Math.sin(angularDistance) * Math.cos(lat1),
      Math.cos(angularDistance) - Math.sin(lat1) * Math.sin(lat2)
    );

  return [toDegrees(lon2), toDegrees(lat2)];
}

function interpolateCoord(a, b, t) {
  return [lerp(a[0], b[0], t), lerp(a[1], b[1], t)];
}

function makeLineString(coordinates) {
  return {
    type: "Feature",
    geometry: {
      type: "LineString",
      coordinates
    },
    properties: {}
  };
}

function makePoint(coord, properties = {}) {
  return {
    type: "Feature",
    geometry: {
      type: "Point",
      coordinates: coord
    },
    properties
  };
}

function routePointCoord(point) {
  return [point.longitude, point.latitude];
}

function buildDensifiedRoute() {
  const sourceCoords = routePoints.map(routePointCoord);
  const coordinates = [];
  const distances = [];
  let totalKm = 0;
  trackDistances = [0];

  for (let i = 1; i < sourceCoords.length; i += 1) {
    trackDistances.push(
      trackDistances[trackDistances.length - 1] +
        haversineKm(sourceCoords[i - 1], sourceCoords[i])
    );
  }

  for (let i = 0; i < sourceCoords.length - 1; i += 1) {
    const start = sourceCoords[i];
    const end = sourceCoords[i + 1];
    const segmentKm = haversineKm(start, end);
    const steps = Math.max(12, Math.ceil(segmentKm / 1.8));

    for (let step = 0; step < steps; step += 1) {
      const t = step / steps;
      const coord = interpolateCoord(start, end, t);
      if (coordinates.length > 0) {
        totalKm += haversineKm(coordinates[coordinates.length - 1], coord);
      }
      coordinates.push(coord);
      distances.push(totalKm);
    }
  }

  const finalCoord = sourceCoords[sourceCoords.length - 1];
  totalKm += haversineKm(coordinates[coordinates.length - 1], finalCoord);
  coordinates.push(finalCoord);
  distances.push(totalKm);

  routeCoordinates = coordinates;
  routeDistances = distances;
  totalRouteDistanceKm = totalKm;
  initialBearing = bearingBetween(routeCoordinates[0], routeCoordinates[1]);
  finalBearing = bearingBetween(
    routeCoordinates[routeCoordinates.length - 2],
    routeCoordinates[routeCoordinates.length - 1]
  );
}

function pointAtDistanceKm(targetDistanceKm) {
  const targetDistance = clamp(targetDistanceKm, 0, totalRouteDistanceKm);
  if (targetDistance <= 0) {
    const lookAheadIndex = Math.min(
      BEARING_LOOK_AHEAD_POINTS,
      routeCoordinates.length - 1
    );
    return {
      coord: routeCoordinates[0],
      nextCoord: routeCoordinates[lookAheadIndex],
      index: 0,
      lookAheadIndex,
      bearing: bearingBetween(routeCoordinates[0], routeCoordinates[lookAheadIndex])
    };
  }

  for (let i = 1; i < routeDistances.length; i += 1) {
    if (routeDistances[i] >= targetDistance) {
      const previousDistance = routeDistances[i - 1];
      const currentDistance = routeDistances[i];
      const segmentT =
        currentDistance === previousDistance
          ? 0
          : (targetDistance - previousDistance) /
            (currentDistance - previousDistance);
      const coord = interpolateCoord(
        routeCoordinates[i - 1],
        routeCoordinates[i],
        segmentT
      );
      const nextIndex = Math.min(i + 1, routeCoordinates.length - 1);
      const lookAheadIndex = Math.min(
        i + BEARING_LOOK_AHEAD_POINTS,
        routeCoordinates.length - 1
      );
      const lookAheadCoord = routeCoordinates[lookAheadIndex];
      return {
        coord,
        nextCoord: routeCoordinates[nextIndex],
        index: i,
        lookAheadIndex,
        bearing:
          lookAheadIndex === i ? finalBearing : bearingBetween(coord, lookAheadCoord)
      };
    }
  }

  const last = routeCoordinates.length - 1;
  return {
    coord: routeCoordinates[last],
    nextCoord: routeCoordinates[last],
    index: last,
    lookAheadIndex: last,
    bearing: finalBearing
  };
}

function pointAlongRoute(fraction) {
  return pointAtDistanceKm(clamp(fraction, 0, 1) * totalRouteDistanceKm);
}

function trackDistanceAt(index) {
  if (trackDistances.length === 0) {
    return 0;
  }
  const clampedIndex = Math.round(clamp(index, 0, trackDistances.length - 1));
  return trackDistances[clampedIndex];
}

function routeFractionForDistance(distanceKm) {
  if (totalRouteDistanceKm <= 0) {
    return 0;
  }
  return clamp(distanceKm / totalRouteDistanceKm, 0, 1);
}

function progressRouteCoordinatesByDistance(distanceKm) {
  const point = pointAtDistanceKm(distanceKm);
  const coords = routeCoordinates.slice(0, Math.max(1, point.index));
  coords.push(point.coord);
  return coords;
}

function progressRouteCoordinates(fraction) {
  return progressRouteCoordinatesByDistance(
    clamp(fraction, 0, 1) * totalRouteDistanceKm
  );
}

function setSourceData(sourceId, data) {
  const source = map.getSource(sourceId);
  if (source) {
    source.setData(data);
  }
}

function mediaPointAtTrackIndex(trackIndex) {
  return mediaPoints.find((point) => point.trackIndex === trackIndex) || null;
}

function labelForTrackIndex(trackIndex, fallbackName = "") {
  const mediaPoint = mediaPointAtTrackIndex(trackIndex);
  if (mediaPoint && mediaPoint.name) {
    return mediaPoint.name;
  }
  return fallbackName || `Track ${trackIndex}`;
}

function mediaMarkerFeatures(activeTrackIndex = null) {
  return {
    type: "FeatureCollection",
    features: mediaPoints.map((point) =>
      makePoint(routePointCoord(routePoints[point.trackIndex]), {
        trackIndex: point.trackIndex,
        label: point.name,
        active: point.trackIndex === activeTrackIndex,
        hasPhotos: Array.isArray(point.photos) && point.photos.length > 0
      })
    )
  };
}

function stopLabelFeature(trackIndex, name, opacity = 0) {
  return makePoint(routePointCoord(routePoints[trackIndex]), {
    label: name || labelForTrackIndex(trackIndex),
    opacity
  });
}

function waitForEvent(target, eventName, timeoutMs = 20000) {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      reject(new Error(`${eventName} timeout`));
    }, timeoutMs);
    target.once(eventName, () => {
      window.clearTimeout(timer);
      resolve();
    });
  });
}

function waitForMapIdle(timeoutMs = 8000) {
  return new Promise((resolve) => {
    if (!map) {
      resolve(false);
      return;
    }

    let done = false;
    const finish = (ok) => {
      if (done) {
        return;
      }
      done = true;
      window.clearTimeout(timer);
      requestAnimationFrame(() => requestAnimationFrame(() => resolve(ok)));
    };

    const timer = window.setTimeout(() => finish(false), timeoutMs);

    if (map.loaded() && map.areTilesLoaded() && map.isStyleLoaded()) {
      finish(true);
      return;
    }

    map.once("idle", () => finish(true));
    map.triggerRepaint();
  });
}

function waitForAnimationFrames(count = 1) {
  return new Promise((resolve) => {
    let remaining = Math.max(1, count);
    const step = () => {
      remaining -= 1;
      if (remaining <= 0) {
        resolve();
        return;
      }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}

function waitForNextRender(timeoutMs = 1200) {
  return new Promise((resolve) => {
    if (!map) {
      resolve(false);
      return;
    }

    let done = false;
    const finish = (ok) => {
      if (done) {
        return;
      }
      done = true;
      window.clearTimeout(timer);
      resolve(ok);
    };

    const timer = window.setTimeout(() => finish(false), timeoutMs);
    map.once("render", () => finish(true));
    map.triggerRepaint();
  });
}

async function waitForFrameRender(timeoutMs = 1200) {
  const renderReady = await waitForNextRender(timeoutMs);
  await waitForAnimationFrames(2);
  return renderReady;
}

async function waitForRenderMode(mode) {
  if (mode === "none") {
    map.triggerRepaint();
    return true;
  }

  if (mode === "raf") {
    map.triggerRepaint();
    await waitForAnimationFrames(Number(window.RENDER_RAF_COUNT) || 1);
    return true;
  }

  return waitForFrameRender(Number(window.FRAME_RENDER_TIMEOUT_MS) || 1200);
}

function addTerrain() {
  try {
    if (!map.getSource(TERRAIN_SOURCE_ID)) {
      map.addSource(TERRAIN_SOURCE_ID, {
        type: "raster-dem",
        url: "mapbox://mapbox.mapbox-terrain-dem-v1",
        tileSize: 512,
        maxzoom: 14
      });
    }
    map.setTerrain({
      source: TERRAIN_SOURCE_ID,
      exaggeration: 1.25
    });
  } catch (error) {
    console.warn(`3D terrain setup skipped: ${error.message}`);
  }
}

// 테마별 설정. lightPreset 은 Standard 스타일 조명(dawn/day/dusk/night),
// snow 는 setSnow(GL JS v3.9+) 파티클 옵션.
// - winter: 초저녁(dusk) + 흰 눈. Mapbox 기본 프리셋(density 0.85,
//   intensity 1.0, vignette 0.3)은 화면을 뒤덮어 과함 → 가볍게 낮춤.
// intensity(낙하 속도)는 wall-clock 기반이라 프레임 캡처 간격(~0.3s)이
// 재생 간격(1/30s)보다 길어 영상에서 빨라 보임 → 낮게 잡는다.
const THEME_PRESETS = {
  winter: {
    lightPreset: "dusk",
    snow: {
      density: 0.15,
      intensity: 0.2,
      "center-thinning": 0.15,
      direction: [0, 50],
      opacity: 0.8,
      color: "#ffffff",
      "flake-size": 0.6,
      vignette: 0.1,
      "vignette-color": "#ffffff"
    }
  }
};

function applyMapTheme() {
  const preset = THEME_PRESETS[MAP_THEME];
  if (!preset) {
    return;
  }
  if (preset.lightPreset) {
    try {
      map.setConfigProperty("basemap", "lightPreset", preset.lightPreset);
      console.info(`[theme] ${MAP_THEME}: lightPreset=${preset.lightPreset}`);
    } catch (error) {
      console.warn(`Theme lightPreset failed: ${error.message}`);
    }
  }
  if (!preset.snow) {
    return;
  }
  if (typeof map.setSnow !== "function") {
    console.warn("map.setSnow unavailable (GL JS < 3.9?); particles skipped.");
    return;
  }
  try {
    map.setSnow(preset.snow);
    console.info(`[theme] ${MAP_THEME}: particles enabled`);
  } catch (error) {
    console.warn(`Theme particles failed: ${error.message}`);
  }
}

function addAtmosphere() {
  // lightPreset 을 바꾸는 테마는 해당 프리셋 자체의 대기/하늘 톤을 살리기
  // 위해 주간용 커스텀 fog 를 씌우지 않는다.
  const preset = THEME_PRESETS[MAP_THEME];
  if (preset && preset.lightPreset) {
    return;
  }
  try {
    map.setFog({
      color: "rgb(204, 226, 255)",
      "high-color": "rgb(64, 114, 180)",
      "horizon-blend": 0.18,
      "space-color": "rgb(8, 18, 36)",
      "star-intensity": 0.12
    });
  } catch (error) {
    console.warn(`Atmosphere setup skipped: ${error.message}`);
  }
}

function addBuildings() {
  try {
    if (!map.getSource("composite")) {
      console.warn("3D buildings source not found; continuing without custom buildings.");
      return;
    }

    if (map.getLayer("custom-3d-buildings")) {
      return;
    }

    const layers = map.getStyle().layers || [];
    const labelLayer = layers.find(
      (layer) =>
        layer.type === "symbol" &&
        layer.layout &&
        layer.layout["text-field"]
    );

    map.addLayer(
      {
        id: "custom-3d-buildings",
        source: "composite",
        "source-layer": "building",
        filter: ["==", ["get", "extrude"], "true"],
        type: "fill-extrusion",
        minzoom: 13,
        paint: {
          "fill-extrusion-color": "#cbd5e1",
          "fill-extrusion-height": [
            "interpolate",
            ["linear"],
            ["zoom"],
            13,
            0,
            15,
            ["coalesce", ["get", "height"], 18]
          ],
          "fill-extrusion-base": [
            "interpolate",
            ["linear"],
            ["zoom"],
            13,
            0,
            15,
            ["coalesce", ["get", "min_height"], 0]
          ],
          "fill-extrusion-opacity": 0.68
        }
      },
      labelLayer ? labelLayer.id : undefined
    );
  } catch (error) {
    console.warn(`3D buildings setup skipped: ${error.message}`);
  }
}

// KTX 느낌의 기차(탑뷰, 정면이 위쪽=북쪽)를 Canvas로 그려 아이콘으로 만든다.
// 나중에 GLB 3D 모델로 교체할 때는 addTrainIcon() + "current-position-train"
// symbol 레이어를 map.addModel + model 레이어로 바꾸면 된다. (bearing 피처
// 속성은 그대로 model-rotation에 쓸 수 있음)
function createTrainIconImage() {
  const pixelRatio = 2;
  const width = 64;
  const height = 232;
  const canvas = document.createElement("canvas");
  canvas.width = width * pixelRatio;
  canvas.height = height * pixelRatio;
  const ctx = canvas.getContext("2d");
  ctx.scale(pixelRatio, pixelRatio);

  const bodyLeft = 14;
  const bodyRight = 50;
  const centerX = (bodyLeft + bodyRight) / 2;

  const frontCarPath = () => {
    ctx.beginPath();
    ctx.moveTo(centerX, 8);
    ctx.quadraticCurveTo(bodyRight, 14, bodyRight, 50);
    ctx.lineTo(bodyRight, 110);
    ctx.quadraticCurveTo(bodyRight, 116, bodyRight - 6, 116);
    ctx.lineTo(bodyLeft + 6, 116);
    ctx.quadraticCurveTo(bodyLeft, 116, bodyLeft, 110);
    ctx.lineTo(bodyLeft, 50);
    ctx.quadraticCurveTo(bodyLeft, 14, centerX, 8);
    ctx.closePath();
  };

  const rearCarPath = () => {
    ctx.beginPath();
    ctx.moveTo(bodyLeft + 7, 122);
    ctx.lineTo(bodyRight - 7, 122);
    ctx.quadraticCurveTo(bodyRight, 122, bodyRight, 129);
    ctx.lineTo(bodyRight, 212);
    ctx.quadraticCurveTo(bodyRight, 224, centerX, 224);
    ctx.quadraticCurveTo(bodyLeft, 224, bodyLeft, 212);
    ctx.lineTo(bodyLeft, 129);
    ctx.quadraticCurveTo(bodyLeft, 122, bodyLeft + 7, 122);
    ctx.closePath();
  };

  // 차량 연결부 (차체 아래에 깔림)
  ctx.fillStyle = "#334155";
  ctx.fillRect(centerX - 10, 112, 20, 16);

  const bodyGradient = ctx.createLinearGradient(bodyLeft, 0, bodyRight, 0);
  bodyGradient.addColorStop(0, "#dbe2ea");
  bodyGradient.addColorStop(0.5, "#f8fafc");
  bodyGradient.addColorStop(1, "#cbd5e1");

  for (const drawPath of [frontCarPath, rearCarPath]) {
    drawPath();
    ctx.strokeStyle = "rgba(255, 255, 255, 0.95)";
    ctx.lineWidth = 6;
    ctx.stroke();
    ctx.fillStyle = bodyGradient;
    ctx.fill();
    ctx.strokeStyle = "#1e293b";
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  // 파란 기수(노즈) 포인트
  frontCarPath();
  ctx.save();
  ctx.clip();
  ctx.fillStyle = "#2563eb";
  ctx.fillRect(bodyLeft, 8, bodyRight - bodyLeft, 12);
  ctx.restore();

  // 전면 유리
  ctx.fillStyle = "#1e293b";
  ctx.beginPath();
  ctx.moveTo(centerX, 20);
  ctx.quadraticCurveTo(bodyRight - 8, 24, bodyRight - 10, 40);
  ctx.lineTo(bodyLeft + 10, 40);
  ctx.quadraticCurveTo(bodyLeft + 8, 24, centerX, 20);
  ctx.closePath();
  ctx.fill();

  // 측면 파란 스트라이프 (KTX 라인)
  ctx.fillStyle = "#2563eb";
  ctx.fillRect(bodyLeft + 2, 48, 4, 64);
  ctx.fillRect(bodyRight - 6, 48, 4, 64);
  ctx.fillRect(bodyLeft + 2, 128, 4, 88);
  ctx.fillRect(bodyRight - 6, 128, 4, 88);

  // 지붕 설비 박스
  ctx.fillStyle = "#cbd5e1";
  ctx.strokeStyle = "#94a3b8";
  ctx.lineWidth = 1;
  for (const [y, h] of [[56, 20], [84, 22], [136, 24], [172, 32]]) {
    ctx.fillRect(centerX - 9, y, 18, h);
    ctx.strokeRect(centerX - 9, y, 18, h);
  }

  return {
    imageData: ctx.getImageData(0, 0, canvas.width, canvas.height),
    pixelRatio
  };
}

function addTrainIcon() {
  if (map.hasImage(TRAIN_ICON_ID)) {
    return;
  }
  const { imageData, pixelRatio } = createTrainIconImage();
  map.addImage(TRAIN_ICON_ID, imageData, { pixelRatio });
}

// Mapbox 내부 로더는 Fetch API 를 쓰는데 fetch 는 file:// 스킴을 지원하지
// 않는다. 그래서 XHR(--allow-file-access-from-files 필요 — render_video.py)로
// 직접 읽어 blob URL 로 변환해 넘긴다. fetch 는 blob: 은 읽을 수 있다.
function loadAssetAsBlobUrl(url) {
  return new Promise((resolve) => {
    try {
      const xhr = new XMLHttpRequest();
      xhr.open("GET", url);
      xhr.responseType = "arraybuffer";
      xhr.onload = () => {
        const ok =
          xhr.status === 200 ||
          (xhr.status === 0 && xhr.response && xhr.response.byteLength > 0);
        if (!ok) {
          resolve(null);
          return;
        }
        const blob = new Blob([xhr.response], { type: "model/gltf-binary" });
        resolve(URL.createObjectURL(blob));
      };
      xhr.onerror = () => resolve(null);
      xhr.send();
    } catch (error) {
      resolve(null);
    }
  });
}

// assets/train.glb 가 있으면 3D 모델을 등록한다. 없거나 실패하면 false 를
// 반환하고 Canvas 기차 아이콘(symbol 레이어)으로 폴백한다.
async function tryAddTrainModel() {
  if (typeof map.addModel !== "function") {
    console.warn("map.addModel unavailable; falling back to train icon.");
    return false;
  }
  for (const url of TRAIN_MODEL_URLS) {
    const blobUrl = await loadAssetAsBlobUrl(url);
    if (!blobUrl) {
      continue;
    }
    try {
      map.addModel(TRAIN_MODEL_ID, blobUrl);
      console.info(`[train] 3D model registered: ${url}`);
      return true;
    } catch (error) {
      console.warn(`Train model registration failed (${url}): ${error.message}`);
    }
  }
  console.warn("Train model unavailable; using train icon.");
  return false;
}

function trainModelScaleForZoom(zoom) {
  const clampedZoom = clamp(Number(zoom) || TRAIN_MODEL_REFERENCE_ZOOM, 0, 22);
  return (
    TRAIN_MODEL_BASE_SCALE * Math.pow(2, TRAIN_MODEL_REFERENCE_ZOOM - clampedZoom)
  );
}

// 매 프레임 진행 방향(bearing, 북 기준 시계방향)과 줌에 맞춰 모델 회전/배율을
// 갱신한다. model-rotation 의 +z 는 시계방향이라 bearing 과 같은 부호로 더한다.
function updateTrainModel(bearingDeg, zoom) {
  if (!trainModelActive || !map.getLayer(TRAIN_MODEL_LAYER_ID)) {
    return;
  }
  const scale = trainModelScaleForZoom(zoom);
  map.setPaintProperty(TRAIN_MODEL_LAYER_ID, "model-rotation", [
    0,
    0,
    normalizeAngle(bearingDeg + TRAIN_MODEL_HEADING_OFFSET_DEG)
  ]);
  map.setPaintProperty(TRAIN_MODEL_LAYER_ID, "model-scale", [scale, scale, scale]);
}

function addRouteLayers() {
  map.addSource(FULL_ROUTE_SOURCE_ID, {
    type: "geojson",
    data: makeLineString(routeCoordinates)
  });

  map.addSource(PROGRESS_ROUTE_SOURCE_ID, {
    type: "geojson",
    data: makeLineString([routeCoordinates[0], routeCoordinates[0]])
  });

  map.addSource(CURRENT_SOURCE_ID, {
    type: "geojson",
    data: makePoint(routeCoordinates[0], {
      bearing: initialBearing
    })
  });

  map.addSource(MARKER_SOURCE_ID, {
    type: "geojson",
    data: mediaMarkerFeatures()
  });

  map.addSource(DESTINATION_LABEL_SOURCE_ID, {
    type: "geojson",
    data: stopLabelFeature(0, "", 0)
  });

  map.addLayer({
    id: "planned-route-line",
    type: "line",
    source: FULL_ROUTE_SOURCE_ID,
    layout: {
      "line-cap": "round",
      "line-join": "round"
    },
    paint: {
      "line-color": "#93c5fd",
      "line-width": 4,
      "line-opacity": 0.42
    }
  });

  map.addLayer({
    id: "progress-route-line",
    type: "line",
    source: PROGRESS_ROUTE_SOURCE_ID,
    layout: {
      "line-cap": "round",
      "line-join": "round"
    },
    paint: {
      "line-color": "#facc15",
      "line-width": 8,
      "line-opacity": 0.95
    }
  });

  map.addLayer({
    id: "route-marker-halo",
    type: "circle",
    source: MARKER_SOURCE_ID,
    paint: {
      "circle-radius": [
        "case",
        ["==", ["get", "active"], true],
        16,
        ["==", ["get", "hasPhotos"], true],
        13,
        11
      ],
      "circle-color": [
        "case",
        ["==", ["get", "active"], true],
        "#ef4444",
        ["==", ["get", "hasPhotos"], true],
        "#f59e0b",
        "#10b981"
      ],
      "circle-opacity": 0.25,
      "circle-stroke-width": 2,
      "circle-stroke-color": "#ffffff"
    }
  });

  map.addLayer({
    id: "route-marker-core",
    type: "circle",
    source: MARKER_SOURCE_ID,
    paint: {
      "circle-radius": [
        "case",
        ["==", ["get", "active"], true],
        9,
        ["==", ["get", "hasPhotos"], true],
        7,
        6
      ],
      "circle-color": [
        "case",
        ["==", ["get", "active"], true],
        "#fb7185",
        ["==", ["get", "hasPhotos"], true],
        "#fbbf24",
        "#34d399"
      ],
      "circle-stroke-width": 2,
      "circle-stroke-color": "#ffffff"
    }
  });

  map.addLayer({
    id: "route-marker-label",
    type: "symbol",
    source: MARKER_SOURCE_ID,
    layout: {
      "text-field": ["get", "label"],
      "text-size": 15,
      "text-font": ["Open Sans Semibold", "Arial Unicode MS Bold"],
      "text-offset": [0, 1.35],
      "text-anchor": "top",
      "text-allow-overlap": true
    },
    paint: {
      "text-color": "#f8fafc",
      "text-halo-color": "#0f172a",
      "text-halo-width": 2,
      "text-opacity": [
        "case",
        ["==", ["get", "active"], true],
        1,
        0.72
      ]
    }
  });

  // 기차가 지면에 붙어 보이도록 바닥 그림자를 깐다 (pitch 기울기에 맞춰 눕힘).
  map.addLayer({
    id: "current-position-shadow",
    type: "circle",
    source: CURRENT_SOURCE_ID,
    paint: {
      "circle-pitch-alignment": "map",
      "circle-radius": [
        "interpolate",
        ["linear"],
        ["zoom"],
        5, 10,
        9, 20,
        12, 28,
        15.5, 38
      ],
      "circle-color": "#0f172a",
      "circle-opacity": 0.22,
      "circle-blur": 0.9
    }
  });

  if (trainModelActive) {
    // 현재 위치 3D 기차 모델. 회전/배율은 updateTrainModel() 이 프레임마다 갱신.
    map.addLayer({
      id: TRAIN_MODEL_LAYER_ID,
      type: "model",
      source: CURRENT_SOURCE_ID,
      layout: {
        "model-id": TRAIN_MODEL_ID
      },
      paint: {
        "model-rotation": [0, 0, 0],
        "model-scale": [
          TRAIN_MODEL_BASE_SCALE,
          TRAIN_MODEL_BASE_SCALE,
          TRAIN_MODEL_BASE_SCALE
        ]
      }
    });
  } else {
    // 폴백: Canvas 기차 아이콘. 지도면에 눕혀(pitch-alignment: map) 진행
    // 방향(bearing)으로 회전시켜 카메라가 기차 뒤를 따라가는 구도를 만든다.
    map.addLayer({
      id: "current-position-train",
      type: "symbol",
      source: CURRENT_SOURCE_ID,
      layout: {
        "icon-image": TRAIN_ICON_ID,
        "icon-size": [
          "interpolate",
          ["linear"],
          ["zoom"],
          5, 0.3,
          9, 0.55,
          12, 0.75,
          15.5, 1.0
        ],
        "icon-rotate": ["coalesce", ["get", "bearing"], 0],
        "icon-rotation-alignment": "map",
        "icon-pitch-alignment": "map",
        "icon-allow-overlap": true,
        "icon-ignore-placement": true
      }
    });
  }

  map.addLayer({
    id: "destination-label",
    type: "symbol",
    source: DESTINATION_LABEL_SOURCE_ID,
    layout: {
      "text-field": ["get", "label"],
      "text-size": 22,
      "text-font": ["Open Sans Bold", "Arial Unicode MS Bold"],
      "text-offset": [0, -2.0],
      "text-anchor": "bottom",
      "text-allow-overlap": true
    },
    paint: {
      "text-color": "#ffffff",
      "text-halo-color": "#111827",
      "text-halo-width": 3,
      "text-opacity": ["coalesce", ["get", "opacity"], 0]
    }
  });
}

function sceneForProgress(progress) {
  const p = clamp(progress, 0, 1);
  const koreaCenter = [127.7669, 36.35];
  const seoul = routePointCoord(routePoints[0]);
  const busan = routePointCoord(routePoints[routePoints.length - 1]);

  if (p < 0.1) {
    const t = easeInOut(p / 0.1);
    return {
      center: interpolateCoord(koreaCenter, [127.65, 36.45], t),
      zoom: lerp(5.15, 5.75, t),
      pitch: lerp(28, 42, t),
      bearing: interpolateAngle(0, 18, t),
      routeIndex: 0,
      lookAheadRouteIndex: BEARING_LOOK_AHEAD_POINTS,
      routeFraction: 0,
      currentCoord: seoul,
      pulse: t,
      labelOpacity: 0
    };
  }

  if (p < 0.2) {
    const t = easeInOut((p - 0.1) / 0.1);
    return {
      center: interpolateCoord([127.65, 36.45], seoul, t),
      zoom: lerp(5.75, 12.2, t),
      pitch: lerp(42, 62, t),
      bearing: interpolateAngle(18, initialBearing, t),
      routeIndex: 0,
      lookAheadRouteIndex: BEARING_LOOK_AHEAD_POINTS,
      routeFraction: 0,
      currentCoord: seoul,
      pulse: 0.35 + 0.65 * t,
      labelOpacity: 0
    };
  }

  if (p < 0.8) {
    const travelT = easeInOut((p - 0.2) / 0.6);
    const point = pointAlongRoute(travelT);
    const zoomOutT = easeInOut(clamp(travelT / 0.18, 0, 1));
    const finishZoomT = easeInOut(clamp((travelT - 0.82) / 0.18, 0, 1));
    const movingZoom = lerp(12.2, 8.25, zoomOutT);
    const zoom = lerp(movingZoom, 10.3, finishZoomT);
    const aheadDistance = lerp(700, 4800, zoomOutT);
    const center = offsetCoordinate(point.coord, point.bearing, aheadDistance);

    return {
      center,
      zoom,
      pitch: lerp(62, 66, zoomOutT),
      bearing: point.bearing,
      routeIndex: point.index,
      lookAheadRouteIndex: point.lookAheadIndex,
      routeFraction: travelT,
      currentCoord: point.coord,
      pulse: (p * 9) % 1,
      labelOpacity: 0
    };
  }

  if (p < 0.92) {
    const t = easeInOut((p - 0.8) / 0.12);
    const point = pointAlongRoute(1);
    const startCenter = offsetCoordinate(point.coord, finalBearing, 1800);
    return {
      center: interpolateCoord(startCenter, busan, t),
      zoom: lerp(10.3, 15.35, t),
      pitch: lerp(66, 68, t),
      bearing: interpolateAngle(finalBearing, finalBearing + 10, t),
      routeIndex: point.index,
      lookAheadRouteIndex: point.lookAheadIndex,
      routeFraction: 1,
      currentCoord: busan,
      pulse: (0.25 + t * 1.8) % 1,
      labelOpacity: clamp(t * 1.2, 0, 1)
    };
  }

  const t = easeInOut((p - 0.92) / 0.08);
  return {
    center: busan,
    zoom: lerp(15.35, 15.55, t),
    pitch: 68,
    bearing: finalBearing + 10,
    routeIndex: routeCoordinates.length - 1,
    lookAheadRouteIndex: routeCoordinates.length - 1,
    routeFraction: 1,
    currentCoord: busan,
    pulse: (0.45 + t * 2.4) % 1,
    labelOpacity: 1
  };
}

function applySceneForProgress(progress, useSmoothing) {
  const scene = sceneForProgress(progress);
  const finalTrackIndex = routePoints.length - 1;
  const finalLabel = labelForTrackIndex(finalTrackIndex);
  const bearingState = useSmoothing
    ? smoothBearingForFrame(
        progress,
        scene.bearing,
        scene.routeIndex,
        scene.lookAheadRouteIndex
      )
    : {
        bearing: normalizeAngle(scene.bearing),
        targetBearing: normalizeAngle(scene.bearing),
        rawTargetBearing: normalizeAngle(scene.bearing),
        angleDelta: 0,
        maxDeltaPerFrame: BEARING_MAX_DEGREES_PER_SECOND / getRenderFps(),
        routeIndex: scene.routeIndex,
        lookAheadIndex: scene.lookAheadRouteIndex
      };

  setSourceData(
    PROGRESS_ROUTE_SOURCE_ID,
    makeLineString(progressRouteCoordinates(scene.routeFraction))
  );
  const trainBearing = pointAlongRoute(scene.routeFraction).bearing;
  setSourceData(
    CURRENT_SOURCE_ID,
    makePoint(scene.currentCoord, {
      bearing: trainBearing
    })
  );
  updateTrainModel(trainBearing, scene.zoom);
  setSourceData(
    MARKER_SOURCE_ID,
    mediaMarkerFeatures(scene.labelOpacity > 0 ? finalTrackIndex : null)
  );
  setSourceData(
    DESTINATION_LABEL_SOURCE_ID,
    stopLabelFeature(finalTrackIndex, finalLabel, scene.labelOpacity)
  );

  map.jumpTo({
    center: scene.center,
    zoom: scene.zoom,
    pitch: scene.pitch,
    bearing: bearingState.bearing
  });

  return {
    scene,
    bearingState
  };
}

// 구간 거리 -> 순항 줌/시야거리. 예전엔 3단계 계단(8.4/9.8/11.7)이라 구간
// 길이가 달라지면 줌이 뚝 바뀌었는데, 로그 스케일 연속 함수로 바꿔 어떤
// 거리 조합이든 순항 줌이 매끄럽게 이어지게 한다. (20km 이하 = 11.7,
// 220km 이상 = 8.4, 사이는 로그 보간 — 기존 3단계와 비슷한 값을 지나간다)
function cruiseBlendForKm(segmentKm) {
  const km = Math.max(Number(segmentKm) || 0, 1);
  return clamp(
    (Math.log(km) - Math.log(20)) / (Math.log(220) - Math.log(20)),
    0,
    1
  );
}

function cruiseZoomForKm(segmentKm) {
  return lerp(11.7, 8.4, cruiseBlendForKm(segmentKm));
}

function cruiseAheadForKm(segmentKm) {
  return lerp(1600, 5200, cruiseBlendForKm(segmentKm));
}

function segmentCameraZoom(segmentKm, progress, settleStart = true, settleEnd = true, entryZoom = null) {
  const zoomOutT = easeInOut(clamp(progress / 0.22, 0, 1));
  // settleEnd === false: skip the end zoom-in so the camera stays at cruise.
  const finishZoomT = settleEnd ? easeInOut(clamp((progress - 0.78) / 0.22, 0, 1)) : 0;
  const cruiseZoom = cruiseZoomForKm(segmentKm);
  // entryZoom: 직전 세그먼트(정지 클로즈업 15.35 나 다른 순항 줌)가 남긴 실제
  // 카메라 줌. 있으면 거기서 이어서 순항 줌으로 보간해 재출발이 튀지 않는다.
  // 없으면(영상 첫 이동) 기존 12.4 줌인 연출 유지.
  const startZoom = entryZoom !== null ? entryZoom : settleStart ? 12.4 : cruiseZoom;
  const movingZoom = lerp(startZoom, cruiseZoom, zoomOutT);
  return lerp(movingZoom, 14.2, finishZoomT);
}

function segmentAheadDistance(segmentKm, progress, settleStart = true, entryAhead = null) {
  const zoomOutT = easeInOut(clamp(progress / 0.22, 0, 1));
  const farDistance = cruiseAheadForKm(segmentKm);
  // entryAhead: 직전 카메라의 look-ahead 거리(정지 = 0). 있으면 이어서 보간해
  // 카메라 중심이 점프하지 않는다.
  const startAhead = entryAhead !== null ? entryAhead : settleStart ? 650 : farDistance;
  return lerp(startAhead, farDistance, zoomOutT);
}

function applyRouteSegment(
  startTrackIndex,
  endTrackIndex,
  segmentProgress,
  settleStart = true,
  settleEnd = true
) {
  const startIndex = Math.round(clamp(startTrackIndex, 0, routePoints.length - 1));
  const endIndex = Math.round(clamp(endTrackIndex, 0, routePoints.length - 1));
  const progress = clamp(segmentProgress, 0, 1);
  const easedProgress = easeInOut(progress);
  const startDistance = trackDistanceAt(startIndex);
  const endDistance = trackDistanceAt(endIndex);
  const distanceKm = lerp(startDistance, endDistance, easedProgress);
  const routeFraction = routeFractionForDistance(distanceKm);
  const point = pointAtDistanceKm(distanceKm);
  const segmentKm = Math.abs(endDistance - startDistance);

  // 세그먼트가 바뀌는 첫 프레임에 직전 카메라 상태를 캡처해 두고, 이 세그먼트
  // 내내 시작값으로 사용한다 (map_pause 는 직전 move 와 같은 인덱스 쌍으로
  // 호출되므로 키가 안 바뀌어 상태가 유지된다).
  const segmentKey = `${startIndex}:${endIndex}`;
  if (segmentKey !== moveEntryKey) {
    moveEntryKey = segmentKey;
    moveEntryState = lastCameraState;
    stopEntryKey = null;
  }
  const entryZoom = moveEntryState ? moveEntryState.zoom : null;
  const entryAhead = moveEntryState ? moveEntryState.ahead : null;
  const entryPitch = moveEntryState ? moveEntryState.pitch : null;

  const aheadDistance = segmentAheadDistance(segmentKm, progress, settleStart, entryAhead);
  const offsetCenter = offsetCoordinate(point.coord, point.bearing, aheadDistance);
  // settleEnd === false: keep looking ahead (don't recenter onto the point) so a
  // photo-less pause stays in the moving framing instead of closing up.
  const finishCenterT = settleEnd
    ? easeInOut(clamp((progress - 0.82) / 0.18, 0, 1)) * 0.45
    : 0;
  const center = interpolateCoord(offsetCenter, point.coord, finishCenterT);
  const bearingState = smoothBearingForFrame(
    routeFraction,
    point.bearing,
    point.index,
    point.lookAheadIndex,
    true
  );

  setSourceData(
    PROGRESS_ROUTE_SOURCE_ID,
    makeLineString(progressRouteCoordinatesByDistance(distanceKm))
  );
  setSourceData(
    CURRENT_SOURCE_ID,
    makePoint(point.coord, {
      bearing: point.bearing
    })
  );
  setSourceData(MARKER_SOURCE_ID, mediaMarkerFeatures(null));
  setSourceData(DESTINATION_LABEL_SOURCE_ID, stopLabelFeature(endIndex, "", 0));

  const cameraZoom = segmentCameraZoom(segmentKm, progress, settleStart, settleEnd, entryZoom);
  updateTrainModel(point.bearing, cameraZoom);

  // 피치도 직전 카메라에서 이어서 순항 피치(67)로 보간.
  const startPitch = entryPitch !== null ? entryPitch : settleStart ? 60 : 67;
  const cameraPitch = lerp(startPitch, 67, easeInOut(clamp(progress / 0.25, 0, 1)));

  map.jumpTo({
    center,
    zoom: cameraZoom,
    pitch: cameraPitch,
    bearing: bearingState.bearing
  });

  lastCameraState = {
    zoom: cameraZoom,
    ahead: aheadDistance,
    pitch: cameraPitch,
    center
  };

  return {
    routeFraction,
    currentCoord: point.coord,
    bearingState,
    startTrackIndex: startIndex,
    endTrackIndex: endIndex,
    routeIndex: point.index,
    lookAheadRouteIndex: point.lookAheadIndex
  };
}

function applyStopPoint(trackIndex, name = "", holdProgress = 1) {
  const index = Math.round(clamp(trackIndex, 0, routePoints.length - 1));
  const distanceKm = trackDistanceAt(index);
  const point = pointAtDistanceKm(distanceKm);
  const routeFraction = routeFractionForDistance(distanceKm);
  const label = name || labelForTrackIndex(index);

  if (smoothedBearing === null) {
    smoothedBearing = normalizeAngle(point.bearing);
  }
  lastProgress = routeFraction;

  // 정지 첫 프레임에 직전 카메라(이동 도착 프레임: 줌 14.2, 중심이 점보다
  // 앞쪽)를 캡처하고, 정지 시간 전반 45% 동안 클로즈업 카메라(15.35, 점
  // 중심)로 이어서 보간한다 — 도착 시 줌/중심이 튀지 않는다.
  if (stopEntryKey !== index) {
    stopEntryKey = index;
    stopEntryState = lastCameraState;
    moveEntryKey = null;
  }
  const settleT = easeInOut(clamp(holdProgress / 0.45, 0, 1));
  const stopCoord = routePointCoord(routePoints[index]);
  const entry = stopEntryState;
  const cameraZoom = lerp(entry ? entry.zoom : 15.35, 15.35, settleT);
  const cameraPitch = lerp(entry ? entry.pitch : 68, 68, settleT);
  const cameraCenter =
    entry && entry.center
      ? interpolateCoord(entry.center, stopCoord, settleT)
      : stopCoord;

  setSourceData(
    PROGRESS_ROUTE_SOURCE_ID,
    makeLineString(progressRouteCoordinatesByDistance(distanceKm))
  );
  setSourceData(
    CURRENT_SOURCE_ID,
    makePoint(routePointCoord(routePoints[index]), {
      bearing: point.bearing
    })
  );
  setSourceData(MARKER_SOURCE_ID, mediaMarkerFeatures(index));
  // 라벨은 카메라 정착에 맞춰 페이드인.
  setSourceData(DESTINATION_LABEL_SOURCE_ID, stopLabelFeature(index, label, settleT));

  updateTrainModel(point.bearing, cameraZoom);

  map.jumpTo({
    center: cameraCenter,
    zoom: cameraZoom,
    pitch: cameraPitch,
    bearing: smoothedBearing
  });

  lastCameraState = {
    zoom: cameraZoom,
    ahead: 0,
    pitch: cameraPitch,
    center: cameraCenter
  };

  return {
    routeFraction,
    trackIndex: index,
    label,
    bearing: smoothedBearing
  };
}

window.renderRouteSegment = async function (
  startTrackIndex,
  endTrackIndex,
  segmentProgress,
  waitMode = "map-render",
  settleStart = true,
  settleEnd = true
) {
  if (!renderReady || !map) {
    throw new Error("Map is not initialized.");
  }

  renderFrameCounter += 1;
  const renderStart = performance.now();
  const scene = applyRouteSegment(
    startTrackIndex,
    endTrackIndex,
    segmentProgress,
    settleStart,
    settleEnd
  );
  const renderEventReady = await waitForRenderMode(waitMode);
  const renderWaitMs = performance.now() - renderStart;

  return {
    startTrackIndex: scene.startTrackIndex,
    endTrackIndex: scene.endTrackIndex,
    progress: segmentProgress,
    routeFraction: scene.routeFraction,
    targetBearing: scene.bearingState.targetBearing,
    rawTargetBearing: scene.bearingState.rawTargetBearing,
    bearing: scene.bearingState.bearing,
    angleDelta: scene.bearingState.angleDelta,
    currentRouteIndex: scene.routeIndex,
    lookAheadRouteIndex: scene.lookAheadRouteIndex,
    renderReady: renderEventReady,
    renderWaitMs
  };
};

window.renderStopPoint = async function (
  trackIndex,
  name = "",
  waitMode = "map-render",
  holdProgress = 1
) {
  if (!renderReady || !map) {
    throw new Error("Map is not initialized.");
  }

  renderFrameCounter += 1;
  const renderStart = performance.now();
  const scene = applyStopPoint(trackIndex, name, holdProgress);
  const renderEventReady = await waitForRenderMode(waitMode);
  const renderWaitMs = performance.now() - renderStart;

  return {
    trackIndex: scene.trackIndex,
    label: scene.label,
    routeFraction: scene.routeFraction,
    bearing: scene.bearing,
    renderReady: renderEventReady,
    renderWaitMs
  };
};

window.initializeMap = async function () {
  if (renderReady) {
    return true;
  }

  if (!window.MAPBOX_ACCESS_TOKEN) {
    throw new Error("MAPBOX_ACCESS_TOKEN is missing.");
  }

  if (!window.mapboxgl) {
    throw new Error("Mapbox GL JS failed to load.");
  }

  mapboxgl.accessToken = window.MAPBOX_ACCESS_TOKEN;
  buildDensifiedRoute();

  map = new mapboxgl.Map({
    container: "map",
    style: MAP_STYLE,
    // globe projection 에서는 3D model 레이어가 아직 렌더링되지 않아 mercator 고정.
    projection: "mercator",
    center: [127.7669, 36.35],
    zoom: 5.15,
    pitch: 28,
    bearing: 0,
    antialias: true,
    preserveDrawingBuffer: true,
    attributionControl: true
  });

  await waitForEvent(map, "load", 45000);
  map.resize();
  applyMapTheme();
  addTerrain();
  addAtmosphere();
  addBuildings();
  trainModelActive = await tryAddTrainModel();
  if (!trainModelActive) {
    addTrainIcon();
  }
  addRouteLayers();
  renderReady = true;
  const idleStart = performance.now();
  const initialIdleReady = await waitForMapIdle(20000);
  const initialIdleMs = performance.now() - idleStart;
  await window.renderFrame(0);
  return {
    ok: true,
    initialIdleReady,
    initialIdleMs,
    routeCoordinateCount: routeCoordinates.length,
    trackPointCount: routePoints.length,
    mediaPointCount: mediaPoints.length
  };
};

window.warmUpRouteTiles = async function (sampleCount = 20) {
  if (!renderReady || !map) {
    throw new Error("Map is not initialized.");
  }

  const started = performance.now();
  let timeoutCount = 0;
  let totalTimeoutHit = false;
  const samples = Math.max(2, Math.round(sampleCount));
  const maxWarmupMs = Number(window.WARMUP_TOTAL_TIMEOUT_MS) || 3000;

  for (let index = 0; index < samples; index += 1) {
    if (performance.now() - started >= maxWarmupMs) {
      totalTimeoutHit = true;
      break;
    }
    const progress = index / (samples - 1);
    applySceneForProgress(progress, false);
    const warmupTimeout = Number(window.WARMUP_IDLE_TIMEOUT_MS) || 500;
    const ok = await waitForMapIdle(warmupTimeout);
    if (!ok) {
      timeoutCount += 1;
      console.warn(`Warmup idle timeout at progress ${progress.toFixed(4)}.`);
    }
  }

  window.resetAnimationState();
  applySceneForProgress(0, false);
  await waitForFrameRender(Number(window.FRAME_RENDER_TIMEOUT_MS) || 300);
  window.resetAnimationState();

  return {
    samples,
    timeoutCount,
    totalTimeoutHit,
    totalMs: performance.now() - started
  };
};

window.renderFrame = async function (progress, waitMode = "map-render") {
  if (!renderReady || !map) {
    throw new Error("Map is not initialized.");
  }

  renderFrameCounter += 1;
  const renderStart = performance.now();
  const { scene, bearingState } = applySceneForProgress(progress, true);
  const renderEventReady = await waitForRenderMode(waitMode);
  const renderWaitMs = performance.now() - renderStart;

  if (!renderEventReady && window.DEBUG_RENDER_TIMEOUTS) {
    console.warn(`Map render timeout at progress ${progress.toFixed(4)}.`);
  }

  return {
    progress,
    routeFraction: scene.routeFraction,
    zoom: scene.zoom,
    pitch: scene.pitch,
    targetBearing: bearingState.targetBearing,
    rawTargetBearing: bearingState.rawTargetBearing,
    bearing: bearingState.bearing,
    angleDelta: bearingState.angleDelta,
    maxDeltaPerFrame: bearingState.maxDeltaPerFrame,
    currentRouteIndex: bearingState.routeIndex,
    lookAheadRouteIndex: bearingState.lookAheadIndex,
    renderReady: renderEventReady,
    renderWaitMs
  };
};

window.getWebGLInfo = function () {
  if (!map) {
    return null;
  }

  const canvas = map.getCanvas();
  const gl =
    canvas.getContext("webgl2") ||
    canvas.getContext("webgl") ||
    canvas.getContext("experimental-webgl");

  const info = {
    vendor: "unknown",
    renderer: "unknown",
    version: "unknown",
    devicePixelRatio: window.devicePixelRatio,
    canvasWidth: canvas.width,
    canvasHeight: canvas.height,
    canvasClientWidth: canvas.clientWidth,
    canvasClientHeight: canvas.clientHeight,
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight
  };

  if (gl) {
    const debugInfo = gl.getExtension("WEBGL_debug_renderer_info");
    info.vendor = debugInfo
      ? gl.getParameter(debugInfo.UNMASKED_VENDOR_WEBGL)
      : gl.getParameter(gl.VENDOR);
    info.renderer = debugInfo
      ? gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL)
      : gl.getParameter(gl.RENDERER);
    info.version = gl.getParameter(gl.VERSION);
  }

  return info;
};

window.isRenderReady = function () {
  return Boolean(renderReady && map && map.isStyleLoaded());
};
