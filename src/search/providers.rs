//! All 6 integrated torrent search providers:
//! 1. The Pirate Bay (`apibay.org`)
//! 2. YTS (`yts.mx`)
//! 3. Nyaa.si (`nyaa.si` RSS)
//! 4. EZTV (`eztv.re`)
//! 5. LimeTorrents (`limetorrents.lol`)
//! 6. 1337x (`1337x.to`)

use std::time::Duration;

use async_trait::async_trait;
use chrono::{TimeZone, Utc};
use futures::future::join_all;
use regex::Regex;
use serde_json::Value;

use crate::search::base::{
    build_magnet, format_bytes, parse_size_to_bytes, SearchCategory, SearchProvider, SearchResult,
};
use crate::torrent::urlencoding_encode;

fn http_client(timeout_secs: f64) -> Option<reqwest::Client> {
    reqwest::Client::builder()
        .timeout(Duration::from_secs_f64(timeout_secs.max(3.0)))
        .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
        .danger_accept_invalid_certs(true)
        .build()
        .ok()
}

// -----------------------------------------------------------------------------
// 1. The Pirate Bay (Apibay JSON API)
// -----------------------------------------------------------------------------

pub struct PirateBaySearchProvider;

#[async_trait]
impl SearchProvider for PirateBaySearchProvider {
    fn name(&self) -> &'static str {
        "The Pirate Bay"
    }

    async fn search(
        &self,
        query: &str,
        category: SearchCategory,
        timeout_secs: f64,
    ) -> Vec<SearchResult> {
        let q = query.trim();
        if q.is_empty() {
            return Vec::new();
        }
        let cat_id = match category {
            SearchCategory::All => "0",
            SearchCategory::Movies => "201",
            SearchCategory::Series => "205",
            SearchCategory::Software => "300",
            SearchCategory::Games => "400",
            SearchCategory::Books => "601",
        };
        let Some(client) = http_client(timeout_secs.min(12.0)) else {
            return Vec::new();
        };
        let url = format!(
            "https://apibay.org/q.php?q={}&cat={}",
            urlencoding_encode(q),
            cat_id
        );
        let Ok(resp) = client.get(&url).send().await else {
            return Vec::new();
        };
        if !resp.status().is_success() {
            return Vec::new();
        }
        let Ok(items) = resp.json::<Vec<Value>>().await else {
            return Vec::new();
        };

        let mut results = Vec::new();
        for item in items {
            let item_id = item
                .get("id")
                .map(|v| v.to_string().trim_matches('"').to_string())
                .unwrap_or_default();
            let name = item
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_string();
            if item_id == "0" && name.to_lowercase().contains("no results") {
                continue;
            }
            let info_hash = item
                .get("info_hash")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_lowercase();
            if info_hash.len() != 40 {
                continue;
            }
            let parse_u64 = |key: &str| -> u64 {
                item.get(key)
                    .and_then(|v| {
                        v.as_u64()
                            .or_else(|| v.as_str().and_then(|s| s.parse::<u64>().ok()))
                    })
                    .unwrap_or(0)
            };
            let seeders = parse_u64("seeders") as u32;
            let leechers = parse_u64("leechers") as u32;
            let size_bytes = parse_u64("size");
            let added_ts = parse_u64("added") as i64;
            let added_date = if added_ts > 0 {
                Utc.timestamp_opt(added_ts, 0)
                    .single()
                    .map(|dt| dt.format("%Y-%m-%d").to_string())
                    .unwrap_or_default()
            } else {
                String::new()
            };

            let cat_display = match category {
                SearchCategory::Movies => "Movies",
                SearchCategory::Series => "TV / Series",
                SearchCategory::Software => "Software",
                SearchCategory::Games => "Games",
                SearchCategory::Books => "E-Books",
                SearchCategory::All => "General",
            }
            .to_string();

            let magnet = build_magnet(&info_hash, &name);
            let source_url = if !item_id.is_empty() && item_id != "0" {
                format!("https://thepiratebay.org/description.php?id={}", item_id)
            } else {
                format!("https://apibay.org/q.php?q={}", urlencoding_encode(&name))
            };
            let torrent_url = format!(
                "https://itorrents.net/torrent/{}.torrent",
                info_hash.to_uppercase()
            );

            results.push(SearchResult {
                title: name,
                info_hash,
                magnet_uri: magnet,
                size_bytes,
                size_formatted: format_bytes(size_bytes),
                seeders,
                leechers,
                category: cat_display,
                provider: self.name().to_string(),
                added_date,
                source_url,
                torrent_url,
                health_score: 0.0,
                age_days: 0,
            });
        }
        results
    }
}

// -----------------------------------------------------------------------------
// 2. YTS (Movies JSON API)
// -----------------------------------------------------------------------------

pub struct YtsSearchProvider;

#[async_trait]
impl SearchProvider for YtsSearchProvider {
    fn name(&self) -> &'static str {
        "YTS"
    }

    async fn search(
        &self,
        query: &str,
        _category: SearchCategory,
        timeout_secs: f64,
    ) -> Vec<SearchResult> {
        let q = query.trim();
        if q.is_empty() {
            return Vec::new();
        }
        let Some(client) = http_client(timeout_secs.min(12.0)) else {
            return Vec::new();
        };
        let url = format!(
            "https://yts.mx/api/v2/list_movies.json?query_term={}&sort_by=seeds&order_by=desc&limit=50",
            urlencoding_encode(q)
        );
        let Ok(resp) = client.get(&url).send().await else {
            return Vec::new();
        };
        if !resp.status().is_success() {
            return Vec::new();
        }
        let Ok(root) = resp.json::<Value>().await else {
            return Vec::new();
        };
        let Some(movies) = root
            .get("data")
            .and_then(|d| d.get("movies"))
            .and_then(|m| m.as_array())
        else {
            return Vec::new();
        };

        let mut results = Vec::new();
        for movie in movies {
            let movie_title = movie
                .get("title")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let movie_url = movie
                .get("url")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let added_date = movie
                .get("date_uploaded")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let Some(torrents) = movie.get("torrents").and_then(|v| v.as_array()) else {
                continue;
            };
            for t in torrents {
                let quality = t.get("quality").and_then(|v| v.as_str()).unwrap_or("");
                let type_str = t.get("type").and_then(|v| v.as_str()).unwrap_or("");
                let title = format!("{} [{}] [{}]", movie_title, quality, type_str)
                    .trim()
                    .to_string();
                let info_hash = t
                    .get("hash")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .trim()
                    .to_lowercase();
                if info_hash.len() != 40 {
                    continue;
                }
                let seeders = t.get("seeds").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
                let leechers = t.get("peers").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
                let size_bytes = t.get("size_bytes").and_then(|v| v.as_u64()).unwrap_or(0);
                let size_disp = t
                    .get("size")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.trim().is_empty())
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| format_bytes(size_bytes));
                let torrent_url = t
                    .get("url")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let magnet = build_magnet(&info_hash, &title);

                results.push(SearchResult {
                    title,
                    info_hash,
                    magnet_uri: magnet,
                    size_bytes,
                    size_formatted: size_disp,
                    seeders,
                    leechers,
                    category: "Movies".to_string(),
                    provider: self.name().to_string(),
                    added_date: added_date.clone(),
                    source_url: movie_url.clone(),
                    torrent_url,
                    health_score: 0.0,
                    age_days: 0,
                });
            }
        }
        results
    }
}

// -----------------------------------------------------------------------------
// 3. Nyaa.si (RSS Feed)
// -----------------------------------------------------------------------------

pub struct NyaaSearchProvider;

#[async_trait]
impl SearchProvider for NyaaSearchProvider {
    fn name(&self) -> &'static str {
        "Nyaa"
    }

    async fn search(
        &self,
        query: &str,
        _category: SearchCategory,
        timeout_secs: f64,
    ) -> Vec<SearchResult> {
        let q = query.trim();
        if q.is_empty() {
            return Vec::new();
        }
        let Some(client) = http_client(timeout_secs.min(12.0)) else {
            return Vec::new();
        };
        let url = format!(
            "https://nyaa.si/?page=rss&q={}&c=0_0&f=0",
            urlencoding_encode(q)
        );
        let Ok(resp) = client.get(&url).send().await else {
            return Vec::new();
        };
        if !resp.status().is_success() {
            return Vec::new();
        }
        let Ok(xml) = resp.text().await else {
            return Vec::new();
        };

        let re_item = Regex::new(r"(?s)<item>(.*?)</item>").unwrap();
        let re_title = Regex::new(r"(?s)<title>(.*?)</title>").unwrap();
        let re_link = Regex::new(r"(?s)<link>(.*?)</link>").unwrap();
        let re_guid = Regex::new(r"(?s)<guid[^>]*>(.*?)</guid>").unwrap();
        let re_hash = Regex::new(r"(?s)<nyaa:infoHash>(.*?)</nyaa:infoHash>").unwrap();
        let re_seed = Regex::new(r"(?s)<nyaa:seeders>(\d+)</nyaa:seeders>").unwrap();
        let re_leech = Regex::new(r"(?s)<nyaa:leechers>(\d+)</nyaa:leechers>").unwrap();
        let re_size = Regex::new(r"(?s)<nyaa:size>(.*?)</nyaa:size>").unwrap();

        let mut results = Vec::new();
        for cap in re_item.captures_iter(&xml) {
            let block = &cap[1];
            let Some(hash_m) = re_hash.captures(block) else {
                continue;
            };
            let info_hash = hash_m[1].trim().to_lowercase();
            if info_hash.len() != 40 {
                continue;
            }
            let title = re_title
                .captures(block)
                .map(|c| c[1].trim().to_string())
                .unwrap_or_default();
            let torrent_url = re_link
                .captures(block)
                .map(|c| c[1].trim().to_string())
                .unwrap_or_default();
            let source_url = re_guid
                .captures(block)
                .map(|c| c[1].trim().to_string())
                .unwrap_or_else(|| torrent_url.clone());
            let seeders = re_seed
                .captures(block)
                .and_then(|c| c[1].parse::<u32>().ok())
                .unwrap_or(0);
            let leechers = re_leech
                .captures(block)
                .and_then(|c| c[1].parse::<u32>().ok())
                .unwrap_or(0);
            let size_disp = re_size
                .captures(block)
                .map(|c| c[1].trim().to_string())
                .unwrap_or_default();
            let size_bytes = parse_size_to_bytes(&size_disp);
            let magnet = build_magnet(&info_hash, &title);

            results.push(SearchResult {
                title,
                info_hash,
                magnet_uri: magnet,
                size_bytes,
                size_formatted: if size_disp.is_empty() {
                    format_bytes(size_bytes)
                } else {
                    size_disp
                },
                seeders,
                leechers,
                category: "Anime".to_string(),
                provider: self.name().to_string(),
                added_date: String::new(),
                source_url,
                torrent_url,
                health_score: 0.0,
                age_days: 0,
            });
        }
        results
    }
}

// -----------------------------------------------------------------------------
// 4. EZTV (`eztv.re`)
// -----------------------------------------------------------------------------

pub struct EztvSearchProvider;

#[async_trait]
impl SearchProvider for EztvSearchProvider {
    fn name(&self) -> &'static str {
        "EZTV"
    }

    async fn search(
        &self,
        query: &str,
        _category: SearchCategory,
        timeout_secs: f64,
    ) -> Vec<SearchResult> {
        let q = query.trim();
        if q.is_empty() {
            return Vec::new();
        }
        let safe_q = Regex::new(r"\s+")
            .unwrap()
            .replace_all(q, "-")
            .trim_matches('-')
            .to_lowercase();
        if safe_q.is_empty() {
            return Vec::new();
        }
        let Some(client) = http_client(timeout_secs.min(12.0)) else {
            return Vec::new();
        };
        let url = format!("https://eztv.re/search/{}", safe_q);
        let Ok(resp) = client.get(&url).send().await else {
            return Vec::new();
        };
        if !resp.status().is_success() {
            return Vec::new();
        }
        let Ok(html) = resp.text().await else {
            return Vec::new();
        };

        let re_row =
            Regex::new(r#"(?is)<tr name="hover" class="forum_header_border">(.*?)</tr>"#).unwrap();
        let re_title = Regex::new(r#"(?is)<a[^>]+class="epinfo"[^>]*>(.*?)</a>"#).unwrap();
        let re_tags = Regex::new(r"<[^>]+>").unwrap();
        let re_mag = Regex::new(r#"(?i)href="(magnet:\?[^"]+)""#).unwrap();
        let re_hash = Regex::new(r"(?i)urn:btih:([a-fA-F0-9]{40})").unwrap();
        let re_seed = Regex::new(r#"(?i)<font color="green">([\d,]+)</font>"#).unwrap();
        let re_size = Regex::new(r"(?i)>\s*([\d.]+\s*[KMGT]B)\s*<").unwrap();

        let mut results = Vec::new();
        for cap in re_row.captures_iter(&html) {
            let row = &cap[1];
            let Some(tm) = re_title.captures(row) else {
                continue;
            };
            let title = re_tags.replace_all(&tm[1], "").trim().to_string();
            let Some(mm) = re_mag.captures(row) else {
                continue;
            };
            let Some(hm) = re_hash.captures(&mm[1]) else {
                continue;
            };
            let info_hash = hm[1].to_lowercase();
            let seeders = re_seed
                .captures(row)
                .and_then(|c| c[1].replace(',', "").parse::<u32>().ok())
                .unwrap_or(0);
            let size_disp = re_size
                .captures(row)
                .map(|c| c[1].trim().to_string())
                .unwrap_or_default();
            let size_bytes = parse_size_to_bytes(&size_disp);
            let magnet = build_magnet(&info_hash, &title);

            results.push(SearchResult {
                title,
                info_hash,
                magnet_uri: magnet,
                size_bytes,
                size_formatted: if size_disp.is_empty() {
                    format_bytes(size_bytes)
                } else {
                    size_disp
                },
                seeders,
                leechers: 0,
                category: "TV / Series".to_string(),
                provider: self.name().to_string(),
                added_date: String::new(),
                source_url: url.clone(),
                torrent_url: String::new(),
                health_score: 0.0,
                age_days: 0,
            });
        }
        results
    }
}

// -----------------------------------------------------------------------------
// 5. LimeTorrents (`limetorrents.lol`)
// -----------------------------------------------------------------------------

pub struct LimeTorrentsSearchProvider;

#[async_trait]
impl SearchProvider for LimeTorrentsSearchProvider {
    fn name(&self) -> &'static str {
        "LimeTorrents"
    }

    async fn search(
        &self,
        query: &str,
        category: SearchCategory,
        timeout_secs: f64,
    ) -> Vec<SearchResult> {
        let q = query.trim();
        if q.is_empty() {
            return Vec::new();
        }
        let cat_slug = match category {
            SearchCategory::All => "all",
            SearchCategory::Movies => "movies",
            SearchCategory::Series => "tv",
            SearchCategory::Software => "applications",
            SearchCategory::Games => "games",
            SearchCategory::Books => "other",
        };
        let safe_q = urlencoding_encode(&q.replace(' ', "-"));
        let url = format!(
            "https://www.limetorrents.lol/search/{}/{}/1/",
            cat_slug, safe_q
        );
        let Some(client) = http_client(timeout_secs.min(12.0)) else {
            return Vec::new();
        };
        let Ok(resp) = client.get(&url).send().await else {
            return Vec::new();
        };
        if !resp.status().is_success() {
            return Vec::new();
        }
        let Ok(html) = resp.text().await else {
            return Vec::new();
        };

        let re_row = Regex::new(
            r#"(?s)<div class="tt-name">.*?href="([^"]*itorrents\.net/torrent/([a-fA-F0-9]{40})\.torrent[^"]*)"[^>]*>.*?<a href="([^"]+)"[^>]*>([^<]+)</a>.*?<td class="tdnormal">(.*?)</td>.*?<td class="tdnormal">([^<]+)</td>.*?<td class="tdseed">([^<]+)</td>.*?<td class="tdleech">([^<]+)</td>"#,
        )
        .unwrap();

        let mut results = Vec::new();
        for cap in re_row.captures_iter(&html) {
            let info_hash = cap[2].trim().to_lowercase();
            let detail_path = cap[3].trim();
            let title = cap[4].trim().to_string();
            let size_str = cap[6].trim().to_string();
            let seeders = cap[7].replace(',', "").trim().parse::<u32>().unwrap_or(0);
            let leechers = cap[8].replace(',', "").trim().parse::<u32>().unwrap_or(0);
            let size_bytes = parse_size_to_bytes(&size_str);
            let magnet = build_magnet(&info_hash, &title);
            let source_url = if detail_path.starts_with('/') {
                format!("https://www.limetorrents.lol{}", detail_path)
            } else {
                detail_path.to_string()
            };
            let torrent_url = format!(
                "https://itorrents.net/torrent/{}.torrent",
                info_hash.to_uppercase()
            );

            results.push(SearchResult {
                title,
                info_hash,
                magnet_uri: magnet,
                size_bytes,
                size_formatted: if size_str.is_empty() {
                    format_bytes(size_bytes)
                } else {
                    size_str
                },
                seeders,
                leechers,
                category: "General".to_string(),
                provider: self.name().to_string(),
                added_date: String::new(),
                source_url,
                torrent_url,
                health_score: 0.0,
                age_days: 0,
            });
        }
        results
    }
}

// -----------------------------------------------------------------------------
// 6. 1337x (`1337x.to`)
// -----------------------------------------------------------------------------

pub struct X1337SearchProvider;

#[async_trait]
impl SearchProvider for X1337SearchProvider {
    fn name(&self) -> &'static str {
        "1337x"
    }

    async fn search(
        &self,
        query: &str,
        _category: SearchCategory,
        timeout_secs: f64,
    ) -> Vec<SearchResult> {
        let q = query.trim();
        if q.is_empty() {
            return Vec::new();
        }
        let url = format!(
            "https://1337x.to/sort-search/{}/seeders/desc/1/",
            urlencoding_encode(q)
        );
        let Some(client) = http_client(timeout_secs.min(12.0)) else {
            return Vec::new();
        };
        let Ok(resp) = client.get(&url).send().await else {
            return Vec::new();
        };
        if !resp.status().is_success() {
            return Vec::new();
        }
        let Ok(html) = resp.text().await else {
            return Vec::new();
        };

        let re_row = Regex::new(r"(?is)<tr[^>]*>(.*?)</tr>").unwrap();
        let re_links = Regex::new(r#"(?is)<a href="(/torrent/[^"]+)"[^>]*>(.*?)</a>"#).unwrap();
        let re_tags = Regex::new(r"<[^>]+>").unwrap();
        let re_seed =
            Regex::new(r#"(?is)<td[^>]*class="[^"]*seeds[^"]*"[^>]*>(\d+)</td>"#).unwrap();
        let re_leech =
            Regex::new(r#"(?is)<td[^>]*class="[^"]*leeches[^"]*"[^>]*>(\d+)</td>"#).unwrap();
        let re_size =
            Regex::new(r#"(?is)<td[^>]*class="[^"]*size[^"]*"[^>]*>(.*?)(?:<span|</td>)"#).unwrap();

        let mut parsed = Vec::new();
        for cap in re_row.captures_iter(&html) {
            let row = &cap[1];
            let Some(lm) = re_links.captures(row) else {
                continue;
            };
            let detail_path = lm[1].to_string();
            let title = re_tags.replace_all(&lm[2], "").trim().to_string();
            let seeders = re_seed
                .captures(row)
                .and_then(|c| c[1].parse::<u32>().ok())
                .unwrap_or(0);
            let leechers = re_leech
                .captures(row)
                .and_then(|c| c[1].parse::<u32>().ok())
                .unwrap_or(0);
            let size_disp = re_size
                .captures(row)
                .map(|c| c[1].trim().to_string())
                .unwrap_or_default();
            parsed.push((title, detail_path, seeders, leechers, size_disp));
            if parsed.len() >= 12 {
                break;
            }
        }

        let re_mag = Regex::new(r#"(?i)href="(magnet:\?[^"]+)""#).unwrap();
        let re_hash = Regex::new(r"(?i)urn:btih:([a-fA-F0-9]{40})").unwrap();

        let futs = parsed
            .into_iter()
            .map(|(title, detail_path, seeders, leechers, size_disp)| {
                let c = client.clone();
                let re_m = re_mag.clone();
                let re_h = re_hash.clone();
                async move {
                    let detail_url = format!("https://1337x.to{}", detail_path);
                    let resp = c.get(&detail_url).send().await.ok()?;
                    if !resp.status().is_success() {
                        return None;
                    }
                    let d_html = resp.text().await.ok()?;
                    let mm = re_m.captures(&d_html)?;
                    let hm = re_h.captures(&mm[1])?;
                    let info_hash = hm[1].to_lowercase();
                    let size_bytes = parse_size_to_bytes(&size_disp);
                    let magnet = build_magnet(&info_hash, &title);
                    Some(SearchResult {
                        title,
                        info_hash,
                        magnet_uri: magnet,
                        size_bytes,
                        size_formatted: if size_disp.is_empty() {
                            format_bytes(size_bytes)
                        } else {
                            size_disp
                        },
                        seeders,
                        leechers,
                        category: "General".to_string(),
                        provider: "1337x".to_string(),
                        added_date: String::new(),
                        source_url: detail_url,
                        torrent_url: String::new(),
                        health_score: 0.0,
                        age_days: 0,
                    })
                }
            });

        join_all(futs).await.into_iter().flatten().collect()
    }
}
