const MAP_STYLE = "mapbox://styles/mapbox/standard";
const TERRAIN_SOURCE_ID = "mapbox-dem";
const FULL_ROUTE_SOURCE_ID = "route-planned";
const PROGRESS_ROUTE_SOURCE_ID = "route-progress";
const CURRENT_SOURCE_ID = "current-position";
const TRAIN_ICON_ID = "train-icon";
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

function addAtmosphere() {
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

  // 현재 위치 기차. 지도면에 눕혀(pitch-alignment: map) 진행 방향(bearing)으로
  // 회전시켜 카메라가 기차 뒤를 따라가는 구도를 만든다.
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
  setSourceData(
    CURRENT_SOURCE_ID,
    makePoint(scene.currentCoord, {
      bearing: pointAlongRoute(scene.routeFraction).bearing
    })
  );
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

function segmentCameraZoom(segmentKm, progress, settleStart = true, settleEnd = true) {
  const zoomOutT = easeInOut(clamp(progress / 0.22, 0, 1));
  // settleEnd === false: skip the end zoom-in so the camera stays at cruise.
  const finishZoomT = settleEnd ? easeInOut(clamp((progress - 0.78) / 0.22, 0, 1)) : 0;
  const cruiseZoom = segmentKm > 180 ? 8.4 : segmentKm > 45 ? 9.8 : 11.7;
  // settleStart === false: begin at cruise (no 12.4 zoom-in flourish) so a move
  // that departs from a photo-less pause doesn't pop bigger before settling.
  const startZoom = settleStart ? 12.4 : cruiseZoom;
  const movingZoom = lerp(startZoom, cruiseZoom, zoomOutT);
  return lerp(movingZoom, 14.2, finishZoomT);
}

function segmentAheadDistance(segmentKm, progress, settleStart = true) {
  const zoomOutT = easeInOut(clamp(progress / 0.22, 0, 1));
  const farDistance = segmentKm > 180 ? 5200 : segmentKm > 45 ? 3600 : 1600;
  // settleStart === false: keep the far look-ahead from the start so the camera
  // center doesn't jump when resuming from a pause.
  const startAhead = settleStart ? 650 : farDistance;
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
  const aheadDistance = segmentAheadDistance(segmentKm, progress, settleStart);
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

  map.jumpTo({
    center,
    zoom: segmentCameraZoom(segmentKm, progress, settleStart, settleEnd),
    // settleStart === false: start at cruise pitch (67) instead of the post-stop
    // 60→67 ramp, so departing a pause doesn't tilt.
    pitch: lerp(settleStart ? 60 : 67, 67, easeInOut(clamp(progress / 0.25, 0, 1))),
    bearing: bearingState.bearing
  });

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

function applyStopPoint(trackIndex, name = "") {
  const index = Math.round(clamp(trackIndex, 0, routePoints.length - 1));
  const distanceKm = trackDistanceAt(index);
  const point = pointAtDistanceKm(distanceKm);
  const routeFraction = routeFractionForDistance(distanceKm);
  const label = name || labelForTrackIndex(index);

  if (smoothedBearing === null) {
    smoothedBearing = normalizeAngle(point.bearing);
  }
  lastProgress = routeFraction;

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
  setSourceData(DESTINATION_LABEL_SOURCE_ID, stopLabelFeature(index, label, 1));

  map.jumpTo({
    center: routePointCoord(routePoints[index]),
    zoom: 15.35,
    pitch: 68,
    bearing: smoothedBearing
  });

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
  waitMode = "map-render"
) {
  if (!renderReady || !map) {
    throw new Error("Map is not initialized.");
  }

  renderFrameCounter += 1;
  const renderStart = performance.now();
  const scene = applyStopPoint(trackIndex, name);
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
  addTerrain();
  addAtmosphere();
  addBuildings();
  addTrainIcon();
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
