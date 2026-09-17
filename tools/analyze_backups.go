// Stream-like structural census for very large SMS Backup & Restore XML files.
//
// The files place base64 payloads inside attributes, sometimes hundreds of MB
// long. Memory mapping lets us inspect those attributes without copying them.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"syscall"
)

type FileReport struct {
	File          string                       `json:"file"`
	FileBytes     int                          `json:"file_bytes"`
	Root          map[string]string            `json:"root_attributes"`
	Tags          map[string]int               `json:"tag_counts"`
	Attributes    map[string]map[string]int    `json:"attributes_by_tag"`
	Values        map[string]map[string]int    `json:"enumerated_values"`
	SubjectKinds  map[string]int               `json:"sms_subject_kinds"`
	PartCounts    map[string]int               `json:"part_counts_by_content_type"`
	PartBytes     map[string]int64             `json:"decoded_attachment_bytes_by_content_type"`
	Base64Chars   map[string]int64             `json:"base64_characters_by_content_type"`
	FirstDate     int64                        `json:"first_message_date_ms"`
	LastDate      int64                        `json:"last_message_date_ms"`
}

func attrs(tag []byte) map[string][]byte {
	out := map[string][]byte{}
	i := bytes.IndexByte(tag, ' ')
	if i < 0 { return out }
	for i < len(tag) {
		for i < len(tag) && (tag[i] == ' ' || tag[i] == '\t' || tag[i] == '\r' || tag[i] == '\n' || tag[i] == '/') { i++ }
		start := i
		for i < len(tag) && tag[i] != '=' && tag[i] != ' ' && tag[i] != '>' { i++ }
		if i == start { break }
		key := string(tag[start:i])
		for i < len(tag) && (tag[i] == ' ' || tag[i] == '=') { i++ }
		if i >= len(tag) || (tag[i] != '"' && tag[i] != '\'') { continue }
		quote := tag[i]; i++
		start = i
		if end := bytes.IndexByte(tag[i:], quote); end >= 0 {
			i += end
			out[key] = tag[start:i]
			i++
		} else { break }
	}
	return out
}

func inc(m map[string]map[string]int, key, value string) {
	if m[key] == nil { m[key] = map[string]int{} }
	m[key][value]++
}

func analyze(path string) (*FileReport, error) {
	f, err := os.Open(path); if err != nil { return nil, err }; defer f.Close()
	st, err := f.Stat(); if err != nil { return nil, err }
	data, err := syscall.Mmap(int(f.Fd()), 0, int(st.Size()), syscall.PROT_READ, syscall.MAP_SHARED)
	if err != nil { return nil, err }
	defer syscall.Munmap(data)
	r := &FileReport{File:path, FileBytes:len(data), Root:map[string]string{}, Tags:map[string]int{},
		Attributes:map[string]map[string]int{}, Values:map[string]map[string]int{}, SubjectKinds:map[string]int{},
		PartCounts:map[string]int{}, PartBytes:map[string]int64{}, Base64Chars:map[string]int64{}, FirstDate:1<<63-1}
	enum := map[string]map[string]bool{
		"sms":{"protocol":true,"type":true,"read":true,"status":true,"locked":true,"sub_id":true},
		"mms":{"msg_box":true,"m_type":true,"read":true,"read_status":true,"sub_id":true,"sim_slot":true},
		"part":{"ct":true,"chset":true}, "addr":{"type":true,"charset":true},
	}
	for pos := 0; pos < len(data); {
		a := bytes.IndexByte(data[pos:], '<'); if a < 0 { break }; a += pos
		b := bytes.IndexByte(data[a:], '>'); if b < 0 { break }; b += a
		tag := data[a+1:b]
		if len(tag)==0 || tag[0]=='/' || tag[0]=='?' || tag[0]=='!' { pos=b+1; continue }
		n := 0; for n<len(tag) && tag[n]!=' ' && tag[n]!='/' { n++ }
		name := string(tag[:n]); if name!="smses" && name!="sms" && name!="mms" && name!="parts" && name!="part" && name!="addrs" && name!="addr" { pos=b+1; continue }
		as := attrs(tag); r.Tags[name]++
		if r.Attributes[name]==nil { r.Attributes[name]=map[string]int{} }
		for k,v := range as {
			r.Attributes[name][k]++
			if enum[name][k] { inc(r.Values, name+"."+k, string(v)) }
		}
		if name=="smses" { for k,v := range as { r.Root[k]=string(v) } }
		if name=="sms" {
			s := string(as["subject"]); kind := "other"
			if s=="" { kind="empty" } else if s=="null" { kind="null" } else if strings.HasPrefix(s,"proto:") { kind="proto" }
			r.SubjectKinds[kind]++
		}
		if name=="sms" || name=="mms" {
			if d,e:=strconv.ParseInt(string(as["date"]),10,64); e==nil {
				if d<r.FirstDate { r.FirstDate=d }; if d>r.LastDate { r.LastDate=d }
			}
		}
		if name=="part" {
			ct:=string(as["ct"]); if ct=="" { ct="(missing)" }; r.PartCounts[ct]++
			if d,ok:=as["data"]; ok && string(d)!="null" {
				l:=int64(len(d)); r.Base64Chars[ct]+=l; pad:=int64(0)
				if len(d)>0 && d[len(d)-1]=='=' { pad++ }; if len(d)>1 && d[len(d)-2]=='=' { pad++ }
				r.PartBytes[ct]+=l*3/4-pad
			}
		}
		pos=b+1
	}
	if r.FirstDate==1<<63-1 { r.FirstDate=0 }
	return r,nil
}

func main() {
	if len(os.Args)<2 { fmt.Fprintln(os.Stderr,"usage: analyze_backups FILE..."); os.Exit(2) }
	out:=struct{Files []*FileReport `json:"files"`}{}
	for _,p:=range os.Args[1:] {
		fmt.Fprintln(os.Stderr,"analyzing",p)
		r,e:=analyze(p); if e!=nil { fmt.Fprintln(os.Stderr,e); os.Exit(1) }; out.Files=append(out.Files,r)
	}
	enc:=json.NewEncoder(os.Stdout); enc.SetIndent("","  "); if err:=enc.Encode(out); err!=nil { panic(err) }
}
