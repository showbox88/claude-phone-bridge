/// <reference path="../pb_data/types.d.ts" />
migrate((app) => {
  const collection = new Collection({
    name: "locations",
    type: "base",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { name: "name", type: "text", required: true, max: 500 },
      { name: "address", type: "text" },
      { name: "city", type: "text" },
      { name: "phone", type: "text" },
      {
        name: "type",
        type: "select",
        maxSelect: 1,
        values: ["餐馆", "超市", "咖啡馆", "酒店", "景点", "商场", "机场/车站", "户外", "其他"],
      },
      {
        name: "rating",
        type: "select",
        maxSelect: 1,
        values: ["⭐", "⭐⭐", "⭐⭐⭐", "⭐⭐⭐⭐", "⭐⭐⭐⭐⭐"],
      },
      { name: "visited", type: "bool" },
      { name: "lat", type: "number" },
      { name: "lng", type: "number" },
      { name: "osm_id", type: "text" },
      { name: "amap_poi_id", type: "text" },
      { name: "created", type: "autodate", onCreate: true, onUpdate: false },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_locations_osm_id ON locations (osm_id) WHERE osm_id != ''",
      "CREATE UNIQUE INDEX idx_locations_amap_poi_id ON locations (amap_poi_id) WHERE amap_poi_id != ''",
      "CREATE INDEX idx_locations_lat_lng ON locations (lat, lng)",
      "CREATE INDEX idx_locations_type ON locations (type)",
      "CREATE INDEX idx_locations_city ON locations (city)",
    ],
  });
  app.save(collection);
}, (app) => {
  const collection = app.findCollectionByNameOrId("locations");
  app.delete(collection);
});
