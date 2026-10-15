//! Keyless and keyed English subtitle providers (YifySubtitles & OpenSubtitles).

use std::collections::HashSet;

use regex::Regex;
use serde::{Deserialize, Serialize};

use crate::torrent::urlencoding_encode;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SubtitleResult {
    pub id: String,
    pub title: String,
    pub language: String,
    pub release: String,
    pub rating: f64,
    pub downloads: u64,
    pub provider: String,
    pub download_url: String,
    pub detail_url: String,
}

const YIFY_BASES: &[&str] = &["https://yifysubtitles.ch", "https://yifysubtitles.org"];

#[derive(Deserialize)]
struct YifyAjaxHit {
    #[serde(default)]
    movie: String,
    #[serde(default)]
    imdb: String,
}

/// YifySubtitles search.
///
/// The legacy server-rendered `/search?q=` page is gone (results now load via
/// JS), so the primary flow is:
/// 1. `GET {base}/ajax/search/?mov=<q>` → JSON `[{movie, imdb}]`
/// 2. `GET {base}/movie-imdb/<imdb>` → rows with `sub-lang">English` + `/subtitles/<slug>` links
/// 3. `GET {base}/subtitles/<slug>` → `/subtitle/<...>.zip` download link
///
/// The old two-step parse is kept as a fallback for mirrors that still
/// render results server-side.
pub async fn search_yify(
    client: &reqwest::Client,
    query: &str,
    limit: usize,
) -> Vec<SubtitleResult> {
    let q = urlencoding_encode(query);
    for &base in YIFY_BASES {
        let results = search_yify_ajax(client, base, &q, limit).await;
        if !results.is_empty() {
            return results;
        }
        let legacy = search_yify_legacy(client, base, &q, limit).await;
        if !legacy.is_empty() {
            return legacy;
        }
    }
    Vec::new()
}

async fn search_yify_ajax(
    client: &reqwest::Client,
    base: &str,
    encoded_query: &str,
    limit: usize,
) -> Vec<SubtitleResult> {
    let mut results = Vec::new();
    let ajax_url = format!("{}/ajax/search/?mov={}", base, encoded_query);
    let Ok(resp) = client
        .get(&ajax_url)
        .header("X-Requested-With", "XMLHttpRequest")
        .header("Referer", format!("{}/", base))
        .send()
        .await
    else {
        return results;
    };
    if !resp.status().is_success() {
        return results;
    }
    let Ok(text) = resp.text().await else {
        return results;
    };
    let hits: Vec<YifyAjaxHit> = serde_json::from_str(&text).unwrap_or_default();

    let mut seen_movies = HashSet::new();
    for hit in hits.iter().take(5) {
        if hit.imdb.is_empty() || !seen_movies.insert(hit.imdb.clone()) {
            continue;
        }
        let detail_url = format!("{}/movie-imdb/{}", base, hit.imdb);
        let Ok(page) = client.get(&detail_url).send().await else {
            continue;
        };
        if !page.status().is_success() {
            continue;
        }
        let Ok(p_html) = page.text().await else {
            continue;
        };
        let title = if hit.movie.trim().is_empty() {
            hit.imdb.clone()
        } else {
            hit.movie.trim().to_string()
        };
        for (sub_path, release) in parse_yify_movie_rows(&p_html) {
            let sub_page_url = format!("{}{}", base, sub_path);
            let Ok(sp_resp) = client.get(&sub_page_url).send().await else {
                continue;
            };
            if !sp_resp.status().is_success() {
                continue;
            }
            let Ok(sp_html) = sp_resp.text().await else {
                continue;
            };
            let Some(zip_path) = parse_yify_zip(&sp_html) else {
                continue;
            };
            let dl = format!("{}{}", base, zip_path);
            results.push(SubtitleResult {
                id: format!("yify:{}", urlencoding_encode(&dl)),
                title: title.clone(),
                language: "en".to_string(),
                release,
                rating: 0.0,
                downloads: 0,
                provider: "yify".to_string(),
                download_url: dl,
                detail_url: sub_page_url,
            });
            if results.len() >= limit {
                return results;
            }
        }
    }
    results
}

/// Parses the new-format movie page: `<tr>` rows whose `sub-lang` is English.
/// Returns `(subtitle page path, release text)` pairs.
fn parse_yify_movie_rows(html: &str) -> Vec<(String, String)> {
    let re_row = Regex::new(r"(?is)<tr[^>]*>(.*?)</tr>").unwrap();
    let re_sub_page = Regex::new(r#"href="(/subtitles/[^"]+)""#).unwrap();
    let re_tags = Regex::new(r"<[^>]+>").unwrap();
    let mut out = Vec::new();
    for row in re_row.captures_iter(html) {
        let body = &row[1];
        if !body.contains("sub-lang\">English") {
            continue;
        }
        let Some(sp) = re_sub_page.captures(body) else {
            continue;
        };
        let release: String = re_tags
            .replace_all(body, " ")
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .chars()
            .take(120)
            .collect();
        out.push((sp[1].to_string(), release));
    }
    out
}

/// Extracts the direct `/subtitle/<...>.zip` href from a subtitle page.
fn parse_yify_zip(html: &str) -> Option<String> {
    let re_zip = Regex::new(r#"href="(/subtitle/[^"]+\.zip)""#).unwrap();
    re_zip.captures(html).map(|cap| cap[1].to_string())
}

async fn search_yify_legacy(
    client: &reqwest::Client,
    base: &str,
    encoded_query: &str,
    limit: usize,
) -> Vec<SubtitleResult> {
    let re_movie = Regex::new(r#"(?is)href="(/movie-imdb/tt\d+)"[^>]*>(.*?)</a>"#).unwrap();
    let re_sub = Regex::new(r#"(?is)href="(/subtitle/[^"]+\.zip)"[^>]*>(.*?)</a>"#).unwrap();
    let re_tags = Regex::new(r"<[^>]+>").unwrap();

    let Ok(resp) = client
        .get(format!("{}/search?q={}", base, encoded_query))
        .send()
        .await
    else {
        return Vec::new();
    };
    if !resp.status().is_success() {
        return Vec::new();
    }
    let Ok(html) = resp.text().await else {
        return Vec::new();
    };

    let mut movies = Vec::new();
    for cap in re_movie.captures_iter(&html) {
        let href = cap[1].to_string();
        let title = re_tags.replace_all(&cap[2], "").trim().to_string();
        if !movies.iter().any(|(h, _)| h == &href) {
            movies.push((
                href,
                if title.is_empty() {
                    encoded_query.to_string()
                } else {
                    title
                },
            ));
        }
        if movies.len() >= 5 {
            break;
        }
    }

    let mut results = Vec::new();
    for (href, title) in movies {
        let detail_url = format!("{}{}", base, href);
        let Ok(page) = client.get(&detail_url).send().await else {
            continue;
        };
        if !page.status().is_success() {
            continue;
        }
        let Ok(p_html) = page.text().await else {
            continue;
        };
        for sm in re_sub.captures_iter(&p_html) {
            let row_all = &sm[0];
            if !row_all.to_lowercase().contains("english") {
                continue;
            }
            let dl = format!("{}{}", base, &sm[1]);
            let cell = re_tags.replace_all(&sm[2], " ").trim().to_string();
            results.push(SubtitleResult {
                id: format!("yify:{}", urlencoding_encode(&dl)),
                title: title.clone(),
                language: "en".to_string(),
                release: cell.chars().take(120).collect(),
                rating: 0.0,
                downloads: 0,
                provider: "yify".to_string(),
                download_url: dl,
                detail_url: detail_url.clone(),
            });
            if results.len() >= limit {
                return results;
            }
        }
    }
    results
}

pub async fn search_opensubtitles_org(
    client: &reqwest::Client,
    query: &str,
    limit: usize,
) -> Vec<SubtitleResult> {
    let url = format!(
        "https://www.opensubtitles.org/en/search2/sublanguageid-eng/moviename-{}",
        urlencoding_encode(query)
    );
    let Ok(resp) = client
        .get(&url)
        .header("Accept-Language", "en")
        .send()
        .await
    else {
        return Vec::new();
    };
    if !resp.status().is_success() {
        return Vec::new();
    }
    let Ok(html) = resp.text().await else {
        return Vec::new();
    };

    let re_row = Regex::new(r#"(?is)href="(/en/subtitles/(\d+)/[^"]*)"[^>]*>(.*?)</a>"#).unwrap();
    let re_tags = Regex::new(r"<[^>]+>").unwrap();

    let mut results = Vec::new();
    for cap in re_row.captures_iter(&html) {
        let detail = format!("https://www.opensubtitles.org{}", &cap[1]);
        let sub_id = &cap[2];
        let title = re_tags.replace_all(&cap[3], "").trim().to_string();
        let dl = format!("https://dl.opensubtitles.org/en/download/sub/{}", sub_id);
        results.push(SubtitleResult {
            id: format!("osorg:{}", sub_id),
            title: if title.is_empty() {
                query.to_string()
            } else {
                title.clone()
            },
            language: "en".to_string(),
            release: title.chars().take(120).collect(),
            rating: 0.0,
            downloads: 0,
            provider: "opensubtitles_org".to_string(),
            download_url: dl,
            detail_url: detail,
        });
        if results.len() >= limit {
            break;
        }
    }
    results
}

/// Keyed OpenSubtitles.com search (only returns results when
/// `OPENSUBTITLES_API_KEY` is set). Free keys at opensubtitles.com.
///
/// Stores the API download endpoint (`/api/v1/download/{file_id}`) as the
/// download URL; `download_for_video` exchanges it for a temporary file link.
pub async fn search_opensubtitles_com(
    client: &reqwest::Client,
    query: &str,
    limit: usize,
) -> Vec<SubtitleResult> {
    let api_key = std::env::var("OPENSUBTITLES_API_KEY").unwrap_or_default();
    if api_key.trim().is_empty() {
        return Vec::new();
    }
    let Ok(resp) = client
        .get("https://api.opensubtitles.com/api/v1/subtitles")
        .query(&[
            ("query", query),
            ("languages", "en"),
            ("order_by", "download_count"),
            ("order_direction", "desc"),
        ])
        .header("Api-Key", api_key.trim())
        .header("Accept", "application/json")
        .send()
        .await
    else {
        return Vec::new();
    };
    if !resp.status().is_success() {
        return Vec::new();
    }
    let Ok(payload): Result<serde_json::Value, _> = resp.json().await else {
        return Vec::new();
    };

    let mut results = Vec::new();
    if let Some(items) = payload.get("data").and_then(|d| d.as_array()) {
        for item in items.iter().take(limit) {
            let attrs = item.get("attributes");
            let files = attrs
                .and_then(|a| a.get("files"))
                .and_then(|f| f.as_array());
            let file_id = files
                .and_then(|f| f.first())
                .and_then(|f| f.get("file_id"))
                .and_then(|v| v.as_u64());
            let movie_name = attrs
                .and_then(|a| a.get("movie_name"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let release = attrs
                .and_then(|a| a.get("release"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let title = if !release.is_empty() {
                release.to_string()
            } else if !movie_name.is_empty() {
                movie_name.to_string()
            } else {
                query.to_string()
            };
            let rating = attrs
                .and_then(|a| a.get("ratings"))
                .and_then(|v| v.as_f64())
                .unwrap_or(0.0);
            let downloads = attrs
                .and_then(|a| a.get("download_count"))
                .and_then(|v| v.as_u64())
                .unwrap_or(0);
            let detail = attrs
                .and_then(|a| a.get("url"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let dl = match file_id {
                Some(id) => format!("https://api.opensubtitles.com/api/v1/download/{}", id),
                None => detail.clone(),
            };
            let id = item
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string();
            results.push(SubtitleResult {
                id: format!("oscom:{}", id),
                title,
                language: "en".to_string(),
                release: release.chars().take(120).collect(),
                rating,
                downloads,
                provider: "opensubtitles_com".to_string(),
                download_url: dl,
                detail_url: detail,
            });
        }
    }
    results
}

#[cfg(test)]
mod tests {
    use super::*;

    // Fixtures captured from the live site (Oct 2026): the legacy
    // server-rendered `/search?q=` page is gone, results are AJAX + rows.
    const AJAX_JSON: &str = r#"[{"movie":"Knock Knock 2015","imdb":"tt3605418"}]"#;

    const MOVIE_ROW: &str = r#"<tr data-id="242360">
        <td class="rating-cell"><span class="label label-success">1</span></td>
        <td class="flag-cell"><span class="flag flag-gb"></span><span class="sub-lang">English</span></td>
        <td><a href="/subtitles/knock-knock-2015-english-yify-242360"><span class="text-muted">subtitle</span> Knock Knock</a></td>
        </tr>
        <tr data-id="242361">
        <td class="flag-cell"><span class="flag flag-fr"></span><span class="sub-lang">French</span></td>
        <td><a href="/subtitles/knock-knock-2015-french-yify-242361">Knock Knock</a></td>
        </tr>"#;

    const SUB_PAGE: &str = r#"<a class="btn-icon download-subtitle" href="/subtitle/knock-knock-2015-english-yify-242360.zip"><span class="title">DOWNLOAD SUBTITLE</span></a>"#;

    #[test]
    fn ajax_hits_parse() {
        let hits: Vec<YifyAjaxHit> = serde_json::from_str(AJAX_JSON).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].imdb, "tt3605418");
        assert_eq!(hits[0].movie, "Knock Knock 2015");
    }

    #[test]
    fn movie_rows_keep_english_only() {
        let rows = parse_yify_movie_rows(MOVIE_ROW);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].0, "/subtitles/knock-knock-2015-english-yify-242360");
        assert!(rows[0].1.contains("Knock Knock"));
    }

    #[test]
    fn subtitle_page_zip_extracts() {
        assert_eq!(
            parse_yify_zip(SUB_PAGE).as_deref(),
            Some("/subtitle/knock-knock-2015-english-yify-242360.zip")
        );
    }
}
