const OVERPASS_URLS = [
  'https://overpass-api.de/api/interpreter',
  'https://overpass.kumi.systems/api/interpreter',
  'https://overpass.openstreetmap.ru/api/interpreter',
]

const WALKABLE = new Set(['residential', 'living_street', 'service', 'unclassified', 'tertiary', 'secondary', 'primary', 'pedestrian', 'footway', 'path', 'cycleway', 'track'])
const HIGH_SPEED = new Set(['motorway', 'motorway_link', 'trunk', 'trunk_link'])

function json(res, status, payload) {
  res.statusCode = status
  res.setHeader('Content-Type', 'application/json; charset=utf-8')
  res.end(JSON.stringify(payload))
}

async function readBody(req) {
  if (req.body && typeof req.body === 'object') return req.body
  const chunks = []
  for await (const chunk of req) chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk))
  const raw = Buffer.concat(chunks).toString('utf8')
  return raw ? JSON.parse(raw) : {}
}

function areaKm2(b) {
  return (b.east - b.west) * 111.32 * Math.max(Math.cos((((b.north + b.south) / 2) * Math.PI) / 180), 0.2) * (b.north - b.south) * 111.32
}

async function fetchOverpass(boundary) {
  const b = boundary
  const query = `
[out:json][timeout:25];
(
  way["highway"](${b.south},${b.west},${b.north},${b.east});
);
out body;
>;
out skel qt;
`
  let lastError = ''
  for (const url of OVERPASS_URLS) {
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
          'User-Agent': 'StreetScope/0.1 research data generator',
        },
        body: new URLSearchParams({ data: query }),
      })
      if (!response.ok) {
        lastError = `${url} ${response.status} ${await response.text().then((text) => text.slice(0, 160)).catch(() => '')}`
        continue
      }
      return response.json()
    } catch (error) {
      lastError = `${url} ${error instanceof Error ? error.message : String(error)}`
    }
  }
  throw new Error(`OSM Overpass request failed: ${lastError}`)
}

function collectRoads(payload, keepWalkable, excludeHighSpeed) {
  const nodes = new Map()
  for (const el of payload.elements || []) {
    if (el.type === 'node' && typeof el.id !== 'undefined') nodes.set(el.id, [Number(el.lon), Number(el.lat)])
  }
  const roads = []
  let index = 0
  for (const way of payload.elements || []) {
    if (way.type !== 'way') continue
    const tags = way.tags || {}
    const highway = String(tags.highway || '')
    if (!highway) continue
    if (excludeHighSpeed && HIGH_SPEED.has(highway)) continue
    if (keepWalkable && !WALKABLE.has(highway)) continue
    const coordinates = (way.nodes || []).map((nodeId) => nodes.get(nodeId)).filter(Boolean)
    if (coordinates.length < 2) continue
    index += 1
    roads.push({
      road_id: `OSM${way.id || index}`,
      road_name: String(tags.name || tags['name:zh'] || tags.ref || `OSM ${highway} ${index}`),
      coordinates,
    })
  }
  return roads
}

function clipSegment(start, end, b) {
  const [x0, y0] = start
  const [x1, y1] = end
  const dx = x1 - x0
  const dy = y1 - y0
  const p = [-dx, dx, -dy, dy]
  const q = [x0 - b.west, b.east - x0, y0 - b.south, b.north - y0]
  let u1 = 0
  let u2 = 1
  for (let i = 0; i < 4; i += 1) {
    if (Math.abs(p[i]) < 1e-15) {
      if (q[i] < 0) return null
      continue
    }
    const t = q[i] / p[i]
    if (p[i] < 0) u1 = Math.max(u1, t)
    else u2 = Math.min(u2, t)
    if (u1 > u2) return null
  }
  const clipped = [
    [Number((x0 + u1 * dx).toFixed(7)), Number((y0 + u1 * dy).toFixed(7))],
    [Number((x0 + u2 * dx).toFixed(7)), Number((y0 + u2 * dy).toFixed(7))],
  ]
  return clipped[0][0] === clipped[1][0] && clipped[0][1] === clipped[1][1] ? null : clipped
}

function clipRoads(roads, boundary) {
  const clipped = []
  for (const road of roads) {
    let part = []
    let partIndex = 0
    const flush = () => {
      if (part.length >= 2) {
        partIndex += 1
        clipped.push({ road_id: partIndex === 1 ? road.road_id : `${road.road_id}_${partIndex}`, road_name: road.road_name, coordinates: part })
      }
      part = []
    }
    for (let i = 0; i < road.coordinates.length - 1; i += 1) {
      const segment = clipSegment(road.coordinates[i], road.coordinates[i + 1], boundary)
      if (!segment) {
        flush()
        continue
      }
      if (!part.length) part.push(segment[0], segment[1])
      else {
        const last = part[part.length - 1]
        if (last[0] === segment[0][0] && last[1] === segment[0][1]) part.push(segment[1])
        else {
          flush()
          part.push(segment[0], segment[1])
        }
      }
    }
    flush()
  }
  return clipped
}

function haversineM(a, b) {
  const radius = 6371000
  const phi1 = (a[1] * Math.PI) / 180
  const phi2 = (b[1] * Math.PI) / 180
  const dphi = ((b[1] - a[1]) * Math.PI) / 180
  const dlambda = ((b[0] - a[0]) * Math.PI) / 180
  const h = Math.sin(dphi / 2) ** 2 + Math.cos(phi1) * Math.cos(phi2) * Math.sin(dlambda / 2) ** 2
  return 2 * radius * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h))
}

function polylineLengthM(coords) {
  let total = 0
  for (let i = 0; i < coords.length - 1; i += 1) total += haversineM(coords[i], coords[i + 1])
  return total
}

function samplePolyline(coords, intervalM) {
  const total = polylineLengthM(coords)
  if (total <= 0) return []
  const samples = []
  for (let target = 0; target <= total; target += intervalM) {
    let walked = 0
    for (let i = 0; i < coords.length - 1; i += 1) {
      const segmentLength = haversineM(coords[i], coords[i + 1])
      if (walked + segmentLength >= target) {
        const ratio = segmentLength === 0 ? 0 : (target - walked) / segmentLength
        samples.push([
          Number((coords[i][0] + (coords[i + 1][0] - coords[i][0]) * ratio).toFixed(7)),
          Number((coords[i][1] + (coords[i + 1][1] - coords[i][1]) * ratio).toFixed(7)),
        ])
        break
      }
      walked += segmentLength
    }
  }
  if (!samples.length) samples.push(coords[0])
  return samples
}

function outOfChina(lng, lat) {
  return !(lng >= 72.004 && lng <= 137.8347 && lat >= 0.8293 && lat <= 55.8271)
}

function transformLat(lng, lat) {
  let ret = -100.0 + 2.0 * lng + 3.0 * lat + 0.2 * lat * lat + 0.1 * lng * lat + 0.2 * Math.sqrt(Math.abs(lng))
  ret += ((20.0 * Math.sin(6.0 * lng * Math.PI) + 20.0 * Math.sin(2.0 * lng * Math.PI)) * 2.0) / 3.0
  ret += ((20.0 * Math.sin(lat * Math.PI) + 40.0 * Math.sin((lat / 3.0) * Math.PI)) * 2.0) / 3.0
  ret += ((160.0 * Math.sin((lat / 12.0) * Math.PI) + 320 * Math.sin((lat * Math.PI) / 30.0)) * 2.0) / 3.0
  return ret
}

function transformLng(lng, lat) {
  let ret = 300.0 + lng + 2.0 * lat + 0.1 * lng * lng + 0.1 * lng * lat + 0.1 * Math.sqrt(Math.abs(lng))
  ret += ((20.0 * Math.sin(6.0 * lng * Math.PI) + 20.0 * Math.sin(2.0 * lng * Math.PI)) * 2.0) / 3.0
  ret += ((20.0 * Math.sin(lng * Math.PI) + 40.0 * Math.sin((lng / 3.0) * Math.PI)) * 2.0) / 3.0
  ret += ((150.0 * Math.sin((lng / 12.0) * Math.PI) + 300.0 * Math.sin((lng / 30.0) * Math.PI)) * 2.0) / 3.0
  return ret
}

function wgs84ToGcj02(lng, lat) {
  if (outOfChina(lng, lat)) return [lng, lat]
  const a = 6378245.0
  const ee = 0.00669342162296594323
  let dlat = transformLat(lng - 105.0, lat - 35.0)
  let dlng = transformLng(lng - 105.0, lat - 35.0)
  const radlat = (lat / 180.0) * Math.PI
  let magic = Math.sin(radlat)
  magic = 1 - ee * magic * magic
  const sqrtmagic = Math.sqrt(magic)
  dlat = (dlat * 180.0) / (((a * (1 - ee)) / (magic * sqrtmagic)) * Math.PI)
  dlng = (dlng * 180.0) / ((a / sqrtmagic) * Math.cos(radlat) * Math.PI)
  return [lng + dlng, lat + dlat]
}

function gcj02ToBd09(lng, lat) {
  const z = Math.sqrt(lng * lng + lat * lat) + 0.00002 * Math.sin((lat * Math.PI * 3000.0) / 180.0)
  const theta = Math.atan2(lat, lng) + 0.000003 * Math.cos((lng * Math.PI * 3000.0) / 180.0)
  return [z * Math.cos(theta) + 0.0065, z * Math.sin(theta) + 0.006]
}

function buildPoint(id, lng, lat, road, intervalM) {
  const [gcjLng, gcjLat] = wgs84ToGcj02(lng, lat)
  const [bdLng, bdLat] = gcj02ToBd09(gcjLng, gcjLat)
  return {
    point_id: id,
    lng,
    lat,
    coord_type: 'wgs84',
    lng_wgs84: lng,
    lat_wgs84: lat,
    lng_gcj02: Number(gcjLng.toFixed(7)),
    lat_gcj02: Number(gcjLat.toFixed(7)),
    lng_bd09: Number(bdLng.toFixed(7)),
    lat_bd09: Number(bdLat.toFixed(7)),
    road_id: road.road_id,
    road_name: road.road_name,
    admin_code: '',
    admin_name: 'OSM 路网',
    sample_interval: intervalM,
    source: 'osm_overpass',
    created_at: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'),
  }
}

function cleanRoads(roads) {
  const seen = new Set()
  const cleaned = []
  let shortRemoved = 0
  let duplicateRemoved = 0
  for (const road of roads) {
    if (polylineLengthM(road.coordinates) < 15) {
      shortRemoved += 1
      continue
    }
    const key = road.coordinates.map((c) => `${c[0].toFixed(6)},${c[1].toFixed(6)}`).join('|')
    if (seen.has(key)) {
      duplicateRemoved += 1
      continue
    }
    seen.add(key)
    cleaned.push(road)
  }
  return { roads: cleaned, report: { raw_roads: roads.length, cleaned_roads: cleaned.length, short_roads_removed: shortRemoved, duplicate_roads_removed: duplicateRemoved } }
}

module.exports = async function handler(req, res) {
  if (req.method === 'OPTIONS') return json(res, 200, {})
  if (req.method !== 'POST') return json(res, 405, { detail: 'Method not allowed' })
  try {
    const body = await readBody(req)
    const boundary = body.boundary || {}
    const intervalM = Math.max(25, Math.min(500, Number(body.interval_m || 100)))
    if (!(boundary.north > boundary.south && boundary.east > boundary.west)) return json(res, 400, { detail: '研究区边界无效' })
    const area = areaKm2(boundary)
    if (area > 80) return json(res, 400, { detail: 'OSM 路网加载区域过大，请缩小到约 80 km² 内' })

    const payload = await fetchOverpass(boundary)
    let roads = clipRoads(collectRoads(payload, body.keep_walkable !== false, body.exclude_high_speed !== false), boundary)
    const cleaning = body.clean_roads === false ? { report: { raw_roads: roads.length, cleaned_roads: roads.length, short_roads_removed: 0, duplicate_roads_removed: 0 } } : cleanRoads(roads)
    roads = cleaning.roads || roads
    if (!roads.length) return json(res, 404, { detail: '当前范围未识别到符合条件的 OSM 路网' })

    const points = []
    for (const road of roads) {
      for (const [lng, lat] of samplePolyline(road.coordinates, intervalM)) {
        points.push(buildPoint(`P${String(points.length + 1).padStart(6, '0')}`, lng, lat, road, intervalM))
      }
    }
    const roadLengthKm = roads.reduce((sum, road) => sum + polylineLengthM(road.coordinates), 0) / 1000
    return json(res, 200, {
      points,
      roads,
      estimate: {
        area_km2: Number(area.toFixed(2)),
        road_length_km: Number(roadLengthKm.toFixed(2)),
        sample_points: points.length,
        four_direction_images: points.length * 4,
        road_cleaning_enabled: body.clean_roads === false ? 0 : 1,
        ...cleaning.report,
      },
      source: 'osm_overpass',
    })
  } catch (error) {
    return json(res, 502, { detail: error instanceof Error ? error.message : String(error) })
  }
}
