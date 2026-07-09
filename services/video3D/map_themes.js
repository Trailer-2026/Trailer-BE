// 지도 계절 테마 공용 모듈 (렌더러 map.html + 빌더 builder.html 이 함께 사용).
//
// window.MapThemes.applyTheme(map, themeName, opts) 하나로 테마를 전환한다.
// - themeName: "default" | "spring" | "summer" | "autumn" | "winter"
//   ("sakura" 는 spring 의 별칭. 빈 문자열/미지정은 default.)
// - "default" 적용 시 파티클 제거·색보정 해제·기본 안개 복원까지 완전히 되돌린다.
// - opts:
//     routeCoordinates: [[lng, lat], ...]  벚꽃나무 배치용 경로 좌표
//     routeLayers: { planned?, progress, casing? }  색을 바꿀 라인 레이어 id
//     treesBeforeId: 나무 심볼 레이어를 이 레이어 아래에 삽입 (라벨 위 덮임 방지)
//
// 새 테마 추가 방법: 아래 THEMES 에 항목 하나 추가하면 끝.
//   { lightPreset?, fog?, colorGrade?, snow?, routeLine?, trees? }
//   빠진 필드는 default 값으로 복원되므로 필요한 것만 적으면 된다.
(function (global) {
  "use strict";

  const TREE_SOURCE_ID = "theme-trees";
  const TREE_LAYER_ID = "theme-trees-symbol";

  // ---------------------------------------------------------------------- //
  // 기본값 (default 테마 = 원래 모습으로 복원할 때 쓰는 값)
  // ---------------------------------------------------------------------- //
  const DEFAULT_FOG = {
    color: "rgb(204, 226, 255)",
    "high-color": "rgb(64, 114, 180)",
    "horizon-blend": 0.18,
    "space-color": "rgb(8, 18, 36)",
    "star-intensity": 0.12
  };

  const DEFAULT_ROUTE_LINE = {
    planned: { color: "#93c5fd", width: 4, opacity: 0.42 },
    progress: { color: "#facc15", width: 8, opacity: 0.95 },
    // 케이싱은 항상 깔려 있고 기본 테마에서는 안 보이게 0 폭.
    casing: { color: "#ffffff", width: 0, opacity: 0 }
  };

  // ---------------------------------------------------------------------- //
  // 벚꽃나무 배치 설정 (튜닝 포인트)
  // ---------------------------------------------------------------------- //
  const SAKURA_TREES = {
    icon: "sakura-tree",
    spacingMeters: [150, 250], // 경로를 따라 나무 간격 (min~max 랜덤)
    offsetMeters: [15, 45], // 경로에서 좌우로 벗어나는 거리 (min~max 랜덤)
    bothSides: true, // 간격 지점마다 선로 양옆에 한 그루씩 심는다
    // 총 나무 수 상한. 경로가 길어 이 수를 넘기면 간격을 자동으로 늘려
    // 경로 전체에 고르게 분산시킨다 (앞부분에만 몰리지 않게).
    maxCount: 600,
    seed: 20260409, // 고정 시드 → 렌더할 때마다 같은 자리에 나무가 심긴다
    // 원경(저줌)에서는 나무가 겹쳐 띠처럼 보여서 숨긴다. 크루즈 줌(~11.7)
    // 에서는 작은 꽃점, 정차 클로즈업에서 또렷한 나무로 보이게.
    minZoom: 10,
    // 줌별 아이콘 크기 (interpolate stop)
    iconSizeStops: [
      [10, 0.12],
      [12, 0.28],
      [14, 0.6],
      [16, 1.0]
    ]
  };

  // ---------------------------------------------------------------------- //
  // 테마 레지스트리
  // lightPreset 은 Standard 스타일 조명(dawn/day/dusk/night),
  // snow 는 setSnow(GL JS v3.9+) 파티클 옵션.
  // - winter: 초저녁(dusk) + 흰 눈. Mapbox 기본 프리셋(density 0.85,
  //   intensity 1.0, vignette 0.3)은 화면을 뒤덮어 과함 → 가볍게 낮춤.
  // - spring: 벚꽃. 눈 파티클을 연분홍으로 물들여 꽃잎 연출 + 파스텔 LUT.
  // intensity(낙하 속도)는 wall-clock 기반이라 프레임 캡처 간격(~0.3s)이
  // 재생 간격(1/30s)보다 길어 영상에서 빨라 보임 → 낮게 잡는다.
  // ---------------------------------------------------------------------- //
  const THEMES = {
    default: {
      // 아무 필드도 없음 = 전부 기본값 복원.
    },
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
    },
    spring: {
      // 조명 프리셋 없음 — 기본 주간 조명 위에 파스텔 색보정 + 벚꽃 파티클.
      lightPreset: null,
      snow: {
        density: 0.15,
        // 줌에 따라 꽃잎 밀도를 자연스럽게 바꾸고 싶으면 아래처럼 interpolate
        // 표현식을 쓸 수 있다 (원경에서는 옅게, 근경에서는 빽빽하게):
        // density: [
        //   "interpolate", ["linear"], ["zoom"],
        //   8, 0.05,
        //   12, 0.15,
        //   15, 0.3
        // ],
        intensity: 0.15,
        "center-thinning": 0.1,
        direction: [40, 65], // [방위각, 고도각] — 옆으로 흩날리는 꽃잎
        opacity: 0.9,
        color: "#ffb7c5",
        "flake-size": 1.0, // 꽃잎은 눈송이보다 크게
        vignette: 0.08,
        "vignette-color": "#ffd7e0"
      },
      // 봄 하늘: 뽀얀 분홍빛 안개 + 부드러운 하늘색.
      fog: {
        color: "rgb(255, 238, 245)",
        "high-color": "rgb(155, 190, 235)",
        "horizon-blend": 0.12,
        "space-color": "rgb(70, 100, 160)",
        "star-intensity": 0
      },
      // 파스텔 벚꽃 색감: 대비를 낮추고 밝기를 올려 파스텔화하고,
      // 초록(녹지·공원·산)을 연분홍(#f9b8d0 계열)으로 밀어 벚꽃 개화 연출.
      // pinkTint 는 도로·건물·물까지 화면 전체에 얹는 분홍 기운 (0~0.15 권장).
      colorGrade: {
        saturation: 0.9,
        contrast: 0.93,
        brightness: 0.04,
        highlightWarmth: 0.08,
        shadowCool: 0.02,
        greenToPink: 0.75,
        pinkTint: 0.07
      },
      // 경로 라인: 메인 핑크(#ff7eb3) + 흰 케이싱으로 귀엽게.
      routeLine: {
        planned: { color: "#f9a8c9", width: 4, opacity: 0.5 },
        progress: { color: "#ff7eb3", width: 8, opacity: 0.95 },
        casing: { color: "#ffffff", width: 13, opacity: 0.85 }
      },
      trees: SAKURA_TREES
    },
    summer: {
      // 일본 여름 애니메이션 톤: 쨍한 한낮 햇빛 + 깊고 새파란 하늘 + 진한 채도.
      // fog 로 하늘을 청량하게 만들고, colorGrade(LUT)로 색감을 끌어올린다.
      lightPreset: null,
      fog: {
        color: "rgb(235, 246, 255)",
        "high-color": "rgb(46, 130, 232)",
        "horizon-blend": 0.14,
        "space-color": "rgb(28, 108, 214)",
        "star-intensity": 0
      },
      colorGrade: {
        saturation: 1.5,
        contrast: 1.12,
        brightness: 0.02,
        highlightWarmth: 0.05,
        shadowCool: 0.06
      }
    },
    autumn: {
      // 가을: 녹지를 단풍빛(호박색)으로 물들이고, 따뜻하고 옅은 안개 낀 하늘에
      // 주황갈색 낙엽이 옆으로 흩날린다.
      lightPreset: null,
      fog: {
        color: "rgb(244, 230, 208)",
        "high-color": "rgb(126, 146, 184)",
        "horizon-blend": 0.12,
        "space-color": "rgb(44, 62, 98)",
        "star-intensity": 0
      },
      colorGrade: {
        saturation: 1.35,
        contrast: 1.08,
        brightness: 0,
        highlightWarmth: 0.12,
        shadowCool: 0.03,
        greenToAmber: 0.8
      },
      snow: {
        density: 0.15,
        intensity: 0.12,
        "center-thinning": 0.1,
        direction: [60, 70],
        opacity: 0.9,
        color: "#d08a3e",
        "flake-size": 1.3,
        vignette: 0.06,
        "vignette-color": "#e8c49a"
      }
    }
  };

  const THEME_ALIASES = { sakura: "spring", "": "default" };

  function normalizeThemeName(name) {
    const key = String(name || "").toLowerCase().trim();
    const resolved = THEME_ALIASES[key] || key;
    return THEMES[resolved] ? resolved : "default";
  }

  // Standard 스타일 시간대 조명. 사용자가 고르면 테마 기본값보다 우선한다.
  const LIGHT_PRESETS = ["dawn", "day", "dusk", "night"];

  function normalizeLightPreset(name) {
    const key = String(name || "").toLowerCase().trim();
    return LIGHT_PRESETS.includes(key) ? key : null;
  }

  // ---------------------------------------------------------------------- //
  // 색보정 LUT
  // 32³ 색상 LUT 를 1024x32 스트립(타일=B 슬라이스, 타일 내 x=R, y=G)으로
  // canvas 에 구워 base64 PNG 로 반환한다. Photoshop LUT 스트립과 동일한 배치.
  // ---------------------------------------------------------------------- //
  function buildColorGradeLUT(grade) {
    const size = 32;
    const canvas = document.createElement("canvas");
    canvas.width = size * size;
    canvas.height = size;
    const ctx = canvas.getContext("2d");
    const image = ctx.createImageData(size * size, size);
    const data = image.data;
    const clamp01 = (value) => Math.min(1, Math.max(0, value));
    for (let b = 0; b < size; b += 1) {
      for (let g = 0; g < size; g += 1) {
        for (let r = 0; r < size; r += 1) {
          let rr = r / (size - 1);
          let gg = g / (size - 1);
          let bb = b / (size - 1);
          // 초록 → 호박색 (가을 단풍): 초록 우세 성분만 붉은 쪽으로 민다.
          if (grade.greenToAmber) {
            const greenness = Math.max(0, gg - Math.max(rr, bb));
            rr += greenness * grade.greenToAmber;
            gg -= greenness * grade.greenToAmber * 0.35;
          }
          // 초록 → 연분홍 (봄 벚꽃): 초록 우세 성분을 분홍 쪽으로 민다.
          if (grade.greenToPink) {
            const greenness = Math.max(0, gg - Math.max(rr, bb));
            rr += greenness * grade.greenToPink;
            bb += greenness * grade.greenToPink * 0.55;
            gg -= greenness * grade.greenToPink * 0.12;
          }
          // 화면 전체에 은은한 분홍 틴트 (밝은 영역일수록 강하게 → 벚꽃빛 도시).
          if (grade.pinkTint) {
            const lightness = (rr + gg + bb) / 3;
            rr += grade.pinkTint * (0.4 + 0.6 * lightness);
            bb += grade.pinkTint * 0.5 * (0.4 + 0.6 * lightness);
            gg -= grade.pinkTint * 0.15;
          }
          // 대비 (0.5 기준)
          rr = 0.5 + (rr - 0.5) * grade.contrast;
          gg = 0.5 + (gg - 0.5) * grade.contrast;
          bb = 0.5 + (bb - 0.5) * grade.contrast;
          // 채도 (luma 기준으로 색 성분만 증폭)
          const luma = 0.2126 * rr + 0.7152 * gg + 0.0722 * bb;
          rr = luma + (rr - luma) * grade.saturation;
          gg = luma + (gg - luma) * grade.saturation;
          bb = luma + (bb - luma) * grade.saturation;
          // 밝기 + 하이라이트는 따뜻하게, 그림자는 푸르게 (애니메이션 색감)
          rr += grade.brightness + grade.highlightWarmth * luma;
          gg += grade.brightness + grade.highlightWarmth * luma * 0.55;
          bb += grade.brightness + grade.shadowCool * (1 - luma);
          const idx = (g * size * size + (b * size + r)) * 4;
          data[idx] = Math.round(clamp01(rr) * 255);
          data[idx + 1] = Math.round(clamp01(gg) * 255);
          data[idx + 2] = Math.round(clamp01(bb) * 255);
          data[idx + 3] = 255;
        }
      }
    }
    ctx.putImageData(image, 0, 0);
    return canvas.toDataURL("image/png").split(",")[1];
  }

  const IDENTITY_GRADE = {
    saturation: 1,
    contrast: 1,
    brightness: 0,
    highlightWarmth: 0,
    shadowCool: 0
  };

  // LUT 색보정을 루트 스타일과 basemap import 양쪽에 적용한다 (Standard 를
  // 직접 로드하면 지도 레이어는 basemap import 밑에 있다).
  // grade=null 이면 항등 LUT 로 색보정을 해제한다.
  function applyColorGrade(map, grade) {
    if (typeof map.setColorTheme !== "function") {
      if (grade) {
        console.warn("map.setColorTheme unavailable; color grade skipped.");
      }
      return;
    }
    const theme = { data: buildColorGradeLUT(grade || IDENTITY_GRADE) };
    try {
      map.setColorTheme(theme);
    } catch (error) {
      console.warn(`Color grade (root) failed: ${error.message}`);
    }
    if (typeof map.setImportColorTheme === "function") {
      try {
        map.setImportColorTheme("basemap", theme);
      } catch (error) {
        // Standard 가 import 로 래핑되지 않은 버전이면 루트 적용만으로 충분.
        console.warn(`Color grade (basemap) skipped: ${error.message}`);
      }
    }
  }

  // ---------------------------------------------------------------------- //
  // 개별 적용 헬퍼 (모두 "테마에 없으면 기본값 복원" 방식)
  // ---------------------------------------------------------------------- //
  function applyLightPreset(map, lightPreset) {
    try {
      map.setConfigProperty("basemap", "lightPreset", lightPreset || "day");
    } catch (error) {
      console.warn(`Theme lightPreset failed: ${error.message}`);
    }
  }

  function applyFog(map, fog) {
    try {
      map.setFog(fog); // null 이면 커스텀 fog 제거 (lightPreset 자체 하늘 사용)
    } catch (error) {
      console.warn(`Theme fog failed: ${error.message}`);
    }
  }

  function applySnow(map, snow) {
    if (typeof map.setSnow !== "function") {
      if (snow) {
        console.warn("map.setSnow unavailable (GL JS < 3.9?); particles skipped.");
      }
      return;
    }
    try {
      map.setSnow(snow || null); // null → 파티클 제거
    } catch (error) {
      console.warn(`Theme particles failed: ${error.message}`);
    }
  }

  function applyLinePaint(map, layerId, style) {
    if (!layerId || !style || !map.getLayer(layerId)) {
      return;
    }
    try {
      map.setPaintProperty(layerId, "line-color", style.color);
      map.setPaintProperty(layerId, "line-width", style.width);
      map.setPaintProperty(layerId, "line-opacity", style.opacity);
    } catch (error) {
      console.warn(`Theme route line (${layerId}) failed: ${error.message}`);
    }
  }

  // defaultRouteLine: 소비자(빌더 등)의 원래 라인 스타일 — default 테마로
  // 되돌릴 때 이 값으로 복원한다. 없으면 렌더러 기본값(DEFAULT_ROUTE_LINE).
  function applyRouteLineStyle(map, routeLine, routeLayers, defaultRouteLine) {
    if (!routeLayers) {
      return;
    }
    const fallback = Object.assign({}, DEFAULT_ROUTE_LINE, defaultRouteLine || {});
    const style = routeLine || {};
    applyLinePaint(map, routeLayers.planned, style.planned || fallback.planned);
    applyLinePaint(map, routeLayers.progress, style.progress || fallback.progress);
    applyLinePaint(map, routeLayers.casing, style.casing || fallback.casing);
  }

  // ---------------------------------------------------------------------- //
  // 벚꽃나무 (경로 주변 장식 심볼)
  // ---------------------------------------------------------------------- //

  // 시드 고정 PRNG — 매 렌더마다 같은 위치에 나무가 심겨 결과가 재현된다.
  function mulberry32(seed) {
    let state = seed >>> 0;
    return function () {
      state = (state + 0x6d2b79f5) >>> 0;
      let t = state;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function haversineMeters(a, b) {
    const R = 6371000;
    const toRad = Math.PI / 180;
    const dLat = (b[1] - a[1]) * toRad;
    const dLng = (b[0] - a[0]) * toRad;
    const lat1 = a[1] * toRad;
    const lat2 = b[1] * toRad;
    const h =
      Math.sin(dLat / 2) ** 2 +
      Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  }

  function segmentBearing(a, b) {
    const toRad = Math.PI / 180;
    const lat1 = a[1] * toRad;
    const lat2 = b[1] * toRad;
    const dLng = (b[0] - a[0]) * toRad;
    const y = Math.sin(dLng) * Math.cos(lat2);
    const x =
      Math.cos(lat1) * Math.sin(lat2) -
      Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLng);
    return (Math.atan2(y, x) * 180) / Math.PI;
  }

  // 좌표에서 bearing(도) 방향으로 meters 만큼 이동한 좌표.
  function offsetCoord(coord, bearingDeg, meters) {
    const rad = (bearingDeg * Math.PI) / 180;
    const dNorth = Math.cos(rad) * meters;
    const dEast = Math.sin(rad) * meters;
    const dLat = dNorth / 111320;
    const dLng = dEast / (111320 * Math.cos((coord[1] * Math.PI) / 180));
    return [coord[0] + dLng, coord[1] + dLat];
  }

  // 경로를 따라 spacing 간격마다, 좌우 offset 만큼 벗어난 지점에 나무 포인트 생성.
  // 예상 나무 수가 maxCount 를 넘으면 간격을 비례로 늘려 경로 전체에 분산한다.
  function generateTreeFeatures(coords, config) {
    const random = mulberry32(config.seed || 1);
    let [minSpacing, maxSpacing] = config.spacingMeters;
    const [minOffset, maxOffset] = config.offsetMeters;
    let totalLength = 0;
    for (let i = 1; i < coords.length; i += 1) {
      totalLength += haversineMeters(coords[i - 1], coords[i]);
    }
    const averageSpacing = (minSpacing + maxSpacing) / 2;
    const treesPerSpot = config.bothSides ? 2 : 1;
    const estimatedCount = (totalLength / averageSpacing) * treesPerSpot;
    if (estimatedCount > config.maxCount) {
      const scale = estimatedCount / config.maxCount;
      minSpacing *= scale;
      maxSpacing *= scale;
    }
    const features = [];
    let nextAt = minSpacing + random() * (maxSpacing - minSpacing);
    let traveled = 0;
    for (let i = 1; i < coords.length && features.length < config.maxCount; i += 1) {
      const start = coords[i - 1];
      const end = coords[i];
      const segLength = haversineMeters(start, end);
      if (segLength <= 0) {
        continue;
      }
      const bearing = segmentBearing(start, end);
      while (traveled + segLength >= nextAt && features.length < config.maxCount) {
        const t = (nextAt - traveled) / segLength;
        const base = [
          start[0] + (end[0] - start[0]) * t,
          start[1] + (end[1] - start[1]) * t
        ];
        // bothSides 면 선로 양옆에 한 그루씩(벚꽃길), 아니면 한쪽 랜덤.
        const sides = config.bothSides ? [-90, 90] : [random() < 0.5 ? -90 : 90];
        for (const side of sides) {
          if (features.length >= config.maxCount) {
            break;
          }
          const offset = minOffset + random() * (maxOffset - minOffset);
          const position = offsetCoord(base, bearing + side, offset);
          features.push({
            type: "Feature",
            geometry: { type: "Point", coordinates: position },
            properties: {}
          });
        }
        nextAt += minSpacing + random() * (maxSpacing - minSpacing);
      }
      traveled += segLength;
    }
    return features;
  }

  // 귀여운 플랫 스타일 벚꽃나무 아이콘 (분홍 꽃송이 뭉치 + 갈색 줄기) —
  // canvas 로 그려 map.addImage 에 등록한다.
  function createTreeIconImage() {
    const pixelRatio = 2;
    const width = 56;
    const height = 64;
    const canvas = document.createElement("canvas");
    canvas.width = width * pixelRatio;
    canvas.height = height * pixelRatio;
    const ctx = canvas.getContext("2d");
    ctx.scale(pixelRatio, pixelRatio);

    // 줄기 (살짝 벌어진 갈색 기둥)
    ctx.fillStyle = "#8d6248";
    ctx.beginPath();
    ctx.moveTo(25, 36);
    ctx.quadraticCurveTo(26, 50, 23, 62);
    ctx.lineTo(33, 62);
    ctx.quadraticCurveTo(30, 50, 31, 36);
    ctx.closePath();
    ctx.fill();
    // 가지 한 줄
    ctx.strokeStyle = "#8d6248";
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(28, 44);
    ctx.quadraticCurveTo(36, 40, 41, 33);
    ctx.stroke();

    // 꽃 뭉치 (겹친 원 5개, 진한 → 연한 분홍)
    const blossoms = [
      { x: 28, y: 20, r: 15, color: "#f9b8d0" },
      { x: 15, y: 26, r: 11, color: "#f7a8c6" },
      { x: 41, y: 26, r: 11, color: "#f7a8c6" },
      { x: 20, y: 13, r: 10, color: "#ffc9dd" },
      { x: 37, y: 14, r: 10, color: "#ffc9dd" }
    ];
    for (const blossom of blossoms) {
      ctx.fillStyle = blossom.color;
      ctx.beginPath();
      ctx.arc(blossom.x, blossom.y, blossom.r, 0, Math.PI * 2);
      ctx.fill();
    }
    // 하이라이트 점 (밝은 꽃송이)
    ctx.fillStyle = "#ffe3ef";
    for (const spot of [
      { x: 23, y: 16, r: 4 },
      { x: 34, y: 22, r: 3.5 },
      { x: 17, y: 24, r: 3 }
    ]) {
      ctx.beginPath();
      ctx.arc(spot.x, spot.y, spot.r, 0, Math.PI * 2);
      ctx.fill();
    }

    return {
      image: ctx.getImageData(0, 0, width * pixelRatio, height * pixelRatio),
      pixelRatio
    };
  }

  function ensureTreeIcon(map, iconName) {
    if (map.hasImage && map.hasImage(iconName)) {
      return;
    }
    const { image, pixelRatio } = createTreeIconImage();
    map.addImage(iconName, image, { pixelRatio });
  }

  function removeTrees(map) {
    try {
      if (map.getLayer(TREE_LAYER_ID)) {
        map.removeLayer(TREE_LAYER_ID);
      }
      if (map.getSource(TREE_SOURCE_ID)) {
        map.removeSource(TREE_SOURCE_ID);
      }
    } catch (error) {
      console.warn(`Theme trees cleanup failed: ${error.message}`);
    }
  }

  function applyTrees(map, config, opts) {
    removeTrees(map);
    if (!config) {
      return;
    }
    const coords = opts.routeCoordinates;
    if (!Array.isArray(coords) || coords.length < 2) {
      return;
    }
    try {
      ensureTreeIcon(map, config.icon);
      const features = generateTreeFeatures(coords, config);
      if (!features.length) {
        return;
      }
      map.addSource(TREE_SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features }
      });
      const sizeExpression = ["interpolate", ["linear"], ["zoom"]];
      for (const [zoom, size] of config.iconSizeStops) {
        sizeExpression.push(zoom, size);
      }
      const beforeId =
        opts.treesBeforeId && map.getLayer(opts.treesBeforeId)
          ? opts.treesBeforeId
          : undefined;
      map.addLayer(
        {
          id: TREE_LAYER_ID,
          type: "symbol",
          source: TREE_SOURCE_ID,
          minzoom: config.minZoom || 0,
          layout: {
            "icon-image": config.icon,
            "icon-size": sizeExpression,
            "icon-anchor": "bottom",
            "icon-allow-overlap": true,
            "icon-ignore-placement": true
          }
        },
        beforeId
      );
      console.info(`[theme] trees planted: ${features.length}`);
    } catch (error) {
      console.warn(`Theme trees failed: ${error.message}`);
    }
  }

  // ---------------------------------------------------------------------- //
  // 진입점
  // ---------------------------------------------------------------------- //
  let activeThemeName = "default";

  function applyTheme(map, themeName, opts = {}) {
    const name = normalizeThemeName(themeName);
    const theme = THEMES[name];
    // opts.lightPreset: 사용자가 고른 시간대 (dawn/day/dusk/night) — 테마보다 우선.
    const overridePreset = normalizeLightPreset(opts.lightPreset);
    const lightPreset = overridePreset || theme.lightPreset;
    applyLightPreset(map, lightPreset);
    // lightPreset 이 day 가 아니면 해당 프리셋 자체의 대기/하늘 톤을 살리기
    // 위해 커스텀 fog 를 씌우지 않는다(null 로 제거).
    let fog;
    if (overridePreset && overridePreset !== "day") {
      fog = null;
    } else {
      fog = theme.fog || (theme.lightPreset ? null : DEFAULT_FOG);
    }
    applyFog(map, fog);
    applyColorGrade(map, theme.colorGrade || null);
    applySnow(map, theme.snow || null);
    applyRouteLineStyle(map, theme.routeLine, opts.routeLayers, opts.defaultRouteLine);
    applyTrees(map, theme.trees || null, opts);
    activeThemeName = name;
    console.info(`[theme] applied: ${name}`);
    return name;
  }

  // 경로가 바뀌었을 때(빌더에서 지점 추가/삭제) 활성 테마의 나무만 다시 심는다.
  function refreshTrees(map, opts = {}) {
    const theme = THEMES[activeThemeName];
    applyTrees(map, theme.trees || null, opts);
  }

  global.MapThemes = {
    THEMES,
    DEFAULT_FOG,
    DEFAULT_ROUTE_LINE,
    applyTheme,
    refreshTrees,
    normalizeThemeName,
    getActiveTheme: () => activeThemeName
  };
})(window);
