// 지도 계절 테마 공용 모듈 (렌더러 map.html + 빌더 builder.html 이 함께 사용).
//
// window.MapThemes.applyTheme(map, themeName, opts) 하나로 테마를 전환한다.
// - themeName: "default" | "spring" | "summer" | "autumn" | "winter"
//   ("sakura" 는 spring 의 별칭. 빈 문자열/미지정은 default.)
// - "default" 적용 시 파티클 제거·색보정 해제·기본 안개 복원까지 완전히 되돌린다.
// - opts:
//     routeLayers: { planned?, progress, casing? }  색을 바꿀 라인 레이어 id
//
// 새 테마 추가 방법: 아래 THEMES 에 항목 하나 추가하면 끝.
//   { lightPreset?, fog?, colorGrade?, snow?, routeLine? }
//   빠진 필드는 default 값으로 복원되므로 필요한 것만 적으면 된다.
(function (global) {
  "use strict";

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
      }
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
      // 가을 낙조: dusk 조명으로 해질녘의 어스름을 살리되, 전역 금빛 틴트
      // (amberTint)와 밝기 보정으로 저녁처럼 어둡지 않게 노랗게 물들인다.
      // 화면 밝기는 lightPreset 이 지배한다 — dawn 은 너무 환하고 dusk 가 노을.
      // 파티클 없음.
      lightPreset: "dusk",
      fog: {
        color: "rgb(255, 150, 74)",         // 지평선의 주황 노을(해질녘 태양빛)
        "high-color": "rgb(196, 106, 116)", // 위로 갈수록 붉은 살구빛 하늘
        "horizon-blend": 0.17,
        "space-color": "rgb(48, 34, 72)",
        "star-intensity": 0.04
      },
      colorGrade: {
        saturation: 1.25,
        contrast: 1.03,
        brightness: 0.07,     // dusk 의 어둠을 한 단계만 걷어냄
        highlightWarmth: 0.2,
        shadowCool: 0.02,
        greenToAmber: 0.8,
        amberTint: 0.12       // 화면 전체에 얹는 노을빛 (아래 LUT 참고)
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
          // 화면 전체에 은은한 금빛 틴트 (밝은 영역일수록 강하게 → 노을빛 도시).
          // 어두운 dusk 장면에서도 노란 기운이 돌게 luma 기반 warmth 를 보완한다.
          if (grade.amberTint) {
            const lightness = (rr + gg + bb) / 3;
            rr += grade.amberTint * (0.4 + 0.6 * lightness);
            gg += grade.amberTint * 0.72 * (0.4 + 0.6 * lightness);
            bb -= grade.amberTint * 0.35;
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
    activeThemeName = name;
    console.info(`[theme] applied: ${name}`);
    return name;
  }

  global.MapThemes = {
    THEMES,
    DEFAULT_FOG,
    DEFAULT_ROUTE_LINE,
    applyTheme,
    normalizeThemeName,
    getActiveTheme: () => activeThemeName
  };
})(window);
