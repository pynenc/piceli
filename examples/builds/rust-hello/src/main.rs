//! A tiny HTTP service used to demonstrate `piceli artifacts build-spec`.
//!
//! `rust-hello` serves `hello from rust-hello` on `0.0.0.0:$PORT` (default
//! 8080). `rust-hello --self-test` binds an ephemeral loopback port, sends one
//! request to itself and exits 0 when the response is correct.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread;

const BODY: &str = "hello from rust-hello\n";

fn handle(mut stream: TcpStream) -> std::io::Result<()> {
    let mut request = [0u8; 1024];
    let _ = stream.read(&mut request)?;
    write!(
        stream,
        "HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
        BODY.len(),
        BODY
    )
}

fn serve(listener: TcpListener, limit: Option<usize>) -> std::io::Result<()> {
    for (count, stream) in listener.incoming().enumerate() {
        handle(stream?)?;
        if limit.is_some_and(|max| count + 1 >= max) {
            break;
        }
    }
    Ok(())
}

fn self_test() -> std::io::Result<()> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let address = listener.local_addr()?;
    let server = thread::spawn(move || serve(listener, Some(1)));
    let mut client = TcpStream::connect(address)?;
    client.write_all(b"GET / HTTP/1.1\r\nhost: localhost\r\n\r\n")?;
    let mut response = String::new();
    client.read_to_string(&mut response)?;
    server.join().expect("server thread")?;
    if response.starts_with("HTTP/1.1 200") && response.ends_with(BODY) {
        println!("self-test ok");
        Ok(())
    } else {
        Err(std::io::Error::other("unexpected response"))
    }
}

fn main() -> std::io::Result<()> {
    match std::env::args().nth(1).as_deref() {
        Some("--version") => {
            println!("rust-hello {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        Some("--self-test") => self_test(),
        _ => {
            let port = std::env::var("PORT").unwrap_or_else(|_| "8080".into());
            let listener = TcpListener::bind(format!("0.0.0.0:{port}"))?;
            serve(listener, None)
        }
    }
}
