use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

use crate::protocol::{IpcRequest, IpcResponse};

#[derive(Debug, Clone)]
pub struct ServiceClient {
    endpoint: String,
}

impl ServiceClient {
    pub fn new(endpoint: impl Into<String>) -> Self { Self { endpoint: endpoint.into() } }
    pub fn endpoint(&self) -> &str { &self.endpoint }

    pub fn request(&self, request: &IpcRequest) -> Result<IpcResponse, String> {
        let mut bytes = serde_json::to_vec(request).map_err(|error| error.to_string())?;
        bytes.push(b'\n');

        #[cfg(unix)]
        {
            use std::os::unix::net::UnixStream;
            let mut stream = UnixStream::connect(&self.endpoint).map_err(|error| error.to_string())?;
            stream.write_all(&bytes).map_err(|error| error.to_string())?;
            stream.flush().map_err(|error| error.to_string())?;
            let mut line = String::new();
            BufReader::new(stream).read_line(&mut line).map_err(|error| error.to_string())?;
            serde_json::from_str(&line).map_err(|error| error.to_string())
        }

        #[cfg(not(unix))]
        {
            use std::net::TcpStream;
            let address = self.endpoint.strip_prefix("tcp://").unwrap_or(&self.endpoint);
            let mut stream = TcpStream::connect(address).map_err(|error| error.to_string())?;
            stream.write_all(&bytes).map_err(|error| error.to_string())?;
            stream.flush().map_err(|error| error.to_string())?;
            let mut line = String::new();
            BufReader::new(stream).read_line(&mut line).map_err(|error| error.to_string())?;
            serde_json::from_str(&line).map_err(|error| error.to_string())
        }
    }
}

pub fn default_endpoint(root: &Path) -> String {
    #[cfg(unix)]
    { root.join(".pipelines/pipecraft.sock").display().to_string() }
    #[cfg(not(unix))]
    { "tcp://127.0.0.1:47831".into() }
}

pub fn endpoint_path(root: &Path) -> PathBuf { root.join(".pipelines/pipecraft.sock") }
