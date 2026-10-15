# Internal v1 data contract

The canonical project is JSON, schema_version = 1. Python >=3.11, standard library runtime.
Design: {schema_version:1, name:string, version:string, board:{width_mm:number,height_mm:number,layers:2|4,manufacturer:"jlcpcb"}, root:Block, connections:[Connection], metadata:{}}.
Block: {id:string, version:string="0.1.0", description:string="", parameters:{}, ports:[Port], children:[Block], components:[Component], depends_on:[absolute block path], equations:{parameter_name:expression}, constraints:[{expression:string,message:string}], frozen:bool=false, frozen_hash:string|null, resources:{}, variants:{}}.
Paths include root id, e.g. board.power.vout (port) or board.power (block). IDs use letters/digits/underscore/hyphen, no dots. Equations are safe arithmetic on numeric local/inherited parameters, and constraints compare these values. No eval or arbitrary code.
Port: {id:string, kind:power|ground|digital|analog|differential|bus, direction:input|output|bidirectional|passive, voltage_min:number|null,voltage_max:number|null,max_current_ma:number|null,current_draw_ma:number=0,logic_standard:string|null,protocol:string|null,required:bool=true,polarity:string|null,impedance_ohm:number|null}.
Connection: {source:absolute port path,target:absolute port path,net:string}.
Component: {id:string,reference:string,value:string,footprint:string,pins:{pin_number:global_net},parameters:{},mpn:string="",lcsc:string="",fitted:bool=true,position:{x_mm:number,y_mm:number,rotation:0,side:"top"}}. Missing physical position is permitted in model but cannot pass manufacturing release.
Physical component pins refer to global nets directly. Connections describe block interfaces. This is a v0.1 explicit mapping, not inferred schematic/library correctness.
Unknown fields, duplicate paths/references, invalid scalar types, NaN/infinity, missing endpoints and invalid schema versions must be rejected. All json dumps deterministic and allow_nan=false. Defaults applied only to omitted optional fields, never invalid supplied values.

Ownership for initial implementation: core agent model.py graph.py expressions.py and tests/test_model.py test_graph.py test_expressions.py; checks agent checks.py reports.py and tests/test_checks.py test_reports.py; KiCad agent backends/* and tests/test_backends.py; root CLI/build/catalog/release/docs/examples/config. Do not edit other owners' files.
